"""
Jahressimulation.py
Jahressimulation des dynamischen EMS fuer eine PV-gepufferte HPC-Ladestation.
Simuliert 01.01.2025 – 31.12.2025 stundengenau.

Szenario A: Dynamische Preissteuerung (Kernlogik.run_simulation)
Szenario B: Statische Schwellensteuerung (Referenz ohne Preisoptimierung)

Voraussetzungen (pip install):
    openmeteo-requests requests-cache retry-requests pandas pytz click
"""

import os
import sys
import re
import pandas as pd
from datetime import datetime, timedelta, date
import requests_cache
import openmeteo_requests
from retry_requests import retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from Kernlogik import (
    load_settings,
    resolve_data_path,
    load_historical_profile_data,
    run_simulation,
    get_dynamic_netzentgelt_ct_kwh,
    get_supermarket_consumption_kw,
)

YEAR = 2025
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


# ── Preise aus CSV laden ───────────────────────────────────────────────────────

def load_year_prices(config) -> pd.DataFrame:
    """
    Laedt Grosshandelspreise aus der Jahres-CSV und gibt einen stuendlichen
    DataFrame mit Spalten [start_time, marketprice_ct_per_kwh] zurueck.

    Unterstuetzt:
    - SMARD-Export (Semikolon, deutsches Datum dd.mm.yyyy HH:MM, 15-min-Aufloesung)
    - ISO-CSV (Komma oder Semikolon, yyyy-mm-dd, stuendlich oder 15-min)
    - Preise in €/MWh oder ct/kWh – wird automatisch erkannt
    15-Minuten-Daten werden auf Stundenmittelwerte resamplet.
    """
    csv_path = resolve_data_path(config, "year_price_file")

    df = None
    for sep in [";", ",", "\t"]:
        try:
            candidate = pd.read_csv(
                csv_path, sep=sep, engine="python",
                encoding="utf-8-sig", thousands=None,
            )
            if len(candidate.columns) >= 2:
                df = candidate
                break
        except Exception:
            continue
    if df is None:
        raise ValueError(f"Preisdatei konnte nicht eingelesen werden: {csv_path}")

    df.columns = [str(c).strip().lower() for c in df.columns]

    # Zeitspalte ermitteln (bevorzuge "datum von" / "start" ueber "bis"-Spalten)
    dt_col = next(
        (c for c in df.columns if any(k in c for k in
            ["datum von", "start", "datum", "date", "zeit", "time", "timestamp"])
         and "bis" not in c),
        df.columns[0],
    )
    # Preisspalte ermitteln – bevorzuge ct/kWh, sonst €/MWh
    price_col = next(
        (c for c in df.columns if "ct/kwh" in c or "ct_kwh" in c),
        None,
    )
    if price_col is None:
        price_col = next(
            (c for c in df.columns if any(k in c for k in
                ["€/mwh", "eur/mwh", "mwh", "preis", "price", "market", "wert", "value"])),
            df.columns[1],
        )

    # Datum parsen – deutsches Format (dayfirst=True) deckt auch ISO ab
    def parse_col(series):
        parsed = pd.to_datetime(series, dayfirst=True, errors="coerce")
        if parsed.isna().mean() > 0.5:
            parsed = pd.to_datetime(series, dayfirst=False, errors="coerce")
        return parsed

    df["start_time"] = parse_col(df[dt_col])

    # Preis parsen: Komma-Dezimal + Tausenderpunkt (deutsches Format)
    raw = df[price_col].astype(str).str.strip()
    raw = raw.apply(lambda s: re.sub(r"[€$\s]", "", s))
    raw = raw.apply(
        lambda s: s.replace(".", "").replace(",", ".") if ("," in s and "." in s)
        else s.replace(",", ".")
    )
    df["marketprice_ct_per_kwh"] = pd.to_numeric(
        raw.str.extract(r"([-+]?\d*\.?\d+)", expand=False), errors="coerce"
    )

    df = df[["start_time", "marketprice_ct_per_kwh"]].dropna().sort_values("start_time")

    # Einheit erkennen: €/MWh (Median > 10) → in ct/kWh umrechnen (÷10)
    # ct/kWh direkt (SMARD-Spalte hat typisch 0–30 ct/kWh)
    median_val = df["marketprice_ct_per_kwh"].abs().median()
    if median_val > 10:
        df["marketprice_ct_per_kwh"] = df["marketprice_ct_per_kwh"] / 10.0
        print(f"  [Preise] Einheit €/MWh erkannt (Median {median_val:.1f}) → ÷10 → ct/kWh")
    else:
        print(f"  [Preise] Einheit ct/kWh erkannt (Median {median_val:.3f} ct/kWh)")

    # Auf Stundenwerte resamplen falls hoehere Aufloesung (z.B. 15-Minuten)
    df = df.set_index("start_time")
    rows_per_hour = df.resample("h").count()["marketprice_ct_per_kwh"].median()
    if rows_per_hour > 1:
        df = df.resample("h").mean()
        print(f"  [Preise] {int(60 / rows_per_hour)}-min-Aufloesung erkannt → auf Stundenmittel resamplet")

    df = df.reset_index().rename(columns={"index": "start_time"})
    df.columns = ["start_time", "marketprice_ct_per_kwh"]
    return df.dropna().reset_index(drop=True)


# ── PV-Jahresdaten aus Open-Meteo Archiv laden ────────────────────────────────

def fetch_pv_year(config, year: int = YEAR, force_refresh: bool = False) -> pd.DataFrame:
    """
    Laed stuendliche GTI-Daten fuer das gesamte Simulationsjahr aus der
    Open-Meteo Archive-API. Ergebnis wird lokal gecacht (CSV) damit
    Folgelaeufe keine API-Anfragen benoetigen.
    """
    cache_path = os.path.join(BASE_DIR, f"pv_{year}_cache.csv")

    if not force_refresh and os.path.exists(cache_path):
        df = pd.read_csv(cache_path, parse_dates=["start_time"])
        print(f"  [PV] Cache geladen: {len(df)} Stunden aus {os.path.basename(cache_path)}")
        return df

    print(f"  [PV] Rufe Open-Meteo Archiv-API fuer {year} ab …")

    tz = config.get("general", "timezone")
    lat = config.getfloat("pv", "lat")
    lon = config.getfloat("pv", "lon")
    installed_kwp = config.getfloat("pv", "installed_kwp")
    pr = config.getfloat("pv", "performance_ratio")

    cache_session = requests_cache.CachedSession(
        os.path.join(BASE_DIR, f".cache_pv_{year}"), expire_after=86400 * 90
    )
    retry_session = retry(cache_session, retries=5, backoff_factor=0.5)
    client = openmeteo_requests.Client(session=retry_session)

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ["global_tilted_irradiance"],
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "timezone": tz,
    }

    resp_ost  = client.weather_api(ARCHIVE_URL, params={**params, "tilt": 15, "azimuth":  88})[0]
    resp_west = client.weather_api(ARCHIVE_URL, params={**params, "tilt": 15, "azimuth": -92})[0]

    hourly   = resp_ost.Hourly()
    gti_ost  = hourly.Variables(0).ValuesAsNumpy()
    gti_west = resp_west.Hourly().Variables(0).ValuesAsNumpy()

    time_range = pd.date_range(
        start=pd.to_datetime(hourly.Time(),    unit="s", utc=True),
        end=  pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
        freq=pd.Timedelta(seconds=hourly.Interval()),
        inclusive="left",
    ).tz_convert(tz).tz_localize(None)

    half_kwp = installed_kwp / 2
    pv_kw = (gti_ost / 1000) * half_kwp * pr + (gti_west / 1000) * half_kwp * pr

    df = pd.DataFrame({"start_time": time_range, "pv_generation_kw": pv_kw})
    df.to_csv(cache_path, index=False)
    print(f"  [PV] {len(df)} Stunden gespeichert: {os.path.basename(cache_path)}")
    return df


# ── Tages-DataFrame bauen (entspricht load_forecast_data_for_day) ─────────────

def build_day_df(
    target_date: date,
    prices_year: pd.DataFrame,
    pv_year: pd.DataFrame,
    config,
) -> pd.DataFrame:
    """
    Erstellt den 24-zeiligen Stunden-DataFrame fuer einen Simulationstag –
    analog zu Kernlogik.load_forecast_data_for_day, aber mit vorgeladenen Daten.
    """
    start_dt = datetime(target_date.year, target_date.month, target_date.day)
    end_dt   = start_dt + timedelta(days=1)

    # Preise fuer diesen Tag
    day_prices = prices_year[
        (prices_year["start_time"] >= start_dt) & (prices_year["start_time"] < end_dt)
    ].copy()

    if len(day_prices) < 24:
        # Fehlende Stunden mit Fallback 25 ct/kWh auffuellen
        full_range = pd.date_range(start=start_dt, periods=24, freq="h")
        fallback = pd.DataFrame({
            "start_time": full_range,
            "marketprice_ct_per_kwh": [25.0] * 24,
        })
        day_prices = (
            fallback.merge(day_prices, on="start_time", how="left", suffixes=("_fb", ""))
        )
        if "marketprice_ct_per_kwh" not in day_prices.columns:
            day_prices["marketprice_ct_per_kwh"] = day_prices.get("marketprice_ct_per_kwh_fb", 25.0)
        day_prices["marketprice_ct_per_kwh"] = day_prices["marketprice_ct_per_kwh"].fillna(25.0)
        day_prices = day_prices[["start_time", "marketprice_ct_per_kwh"]].sort_values("start_time")

    df = day_prices.rename(columns={"marketprice_ct_per_kwh": "price_ct_kwh"}).copy()
    df["hour"] = df["start_time"].dt.hour

    # Dynamisches Netzentgelt + fixe Kostenbestandteile
    df["netzentgelt_ct_kwh"] = df["hour"].apply(lambda h: get_dynamic_netzentgelt_ct_kwh(h, config))
    df["price_ct_kwh"] = (
        df["price_ct_kwh"]
        + df["netzentgelt_ct_kwh"]
        + config.getfloat("pricing", "p_stromsteuer_ct_kwh")
        + config.getfloat("pricing", "p_umlagen_ct_kwh")
        + config.getfloat("pricing", "p_vertrieb_ct_kwh")
    ) * config.getfloat("pricing", "mwst_factor")

    # PV-Daten fuer diesen Tag
    day_pv = pv_year[
        (pv_year["start_time"] >= start_dt) & (pv_year["start_time"] < end_dt)
    ][["start_time", "pv_generation_kw"]]
    df = pd.merge(df, day_pv, on="start_time", how="left")
    df["pv_generation_kw"] = df["pv_generation_kw"].fillna(0.0)

    # Supermarktlast
    df["supermarket_consumption_kw"] = df["hour"].apply(
        lambda h: get_supermarket_consumption_kw(h, config)
    )
    df["month"]   = df["start_time"].dt.month
    df["weekday"] = df["start_time"].dt.dayofweek

    return df.reset_index(drop=True)


# ── Szenario A: Jahreslauf mit Kernlogik ──────────────────────────────────────

def run_year_simulation_dynamic(
    config,
    prices_df: pd.DataFrame,
    pv_df: pd.DataFrame,
    consumption_lookup: dict,
    scenario_name: str = "Szenario_A_Dynamisch",
    initial_soc: float = 50.0,
) -> pd.DataFrame:
    """
    Fuehrt die vollstaendige Jahressimulation mit der dynamischen Kernlogik durch.
    End-SOC eines Tages = Start-SOC des naechsten Tages.
    """
    all_days = []
    current_soc = float(initial_soc)
    year_dates  = pd.date_range(f"{YEAR}-01-01", f"{YEAR}-12-31", freq="D")

    print(f"\n  {'─'*56}")
    print(f"  Szenario: {scenario_name}")
    print(f"  Start-SOC: {current_soc:.1f}%  |  Tage: {len(year_dates)}")
    print(f"  {'─'*56}")

    for i, ts in enumerate(year_dates):
        day = ts.date()
        if i % 30 == 0:
            print(f"    Tag {i+1:3d}/365: {day}  |  SOC: {current_soc:5.1f}%")

        day_raw = build_day_df(day, prices_df, pv_df, config)
        day_sim = run_simulation(day_raw, consumption_lookup, initial_soc=current_soc, config=config)

        current_soc = float(day_sim["ist_soc"].iloc[-1])
        day_sim["date"]     = str(day)
        day_sim["scenario"] = scenario_name
        all_days.append(day_sim)

    print(f"    End-SOC: {current_soc:.1f}%")
    return pd.concat(all_days, ignore_index=True)


# ── Szenario B: Statische Schwellensteuerung (Referenz) ───────────────────────

STATIC_TARGET_SOC   = 60.0   # Fester Soll-SOC Szenario B
STATIC_PRICE_CT_KWH = 34.0   # Festpreis Szenario B (ct/kWh brutto)


def _simulate_day_static(df_raw, consumption_lookup, initial_soc, config) -> pd.DataFrame:
    """
    Referenz-Steuerung (Szenario B):
    - Soll-SOC: 60 % (fest, kein Preis-Lookahead, keine Bedarfsprognose)
    - Netzladung: wenn ist_soc < 60 %, sonst keine Aktion
    - PV-Ladung: nur wenn PV-Ueberschuss vorhanden, wird nicht fest eingeplant
    - Preis: 34 ct/kWh (Festpreis, bereits im DataFrame gesetzt)
    """
    capacity_kwh   = config.getfloat("battery", "capacity_kwh")
    max_charge_kw  = config.getfloat("battery", "max_charge_kw")
    efficiency     = config.getfloat("battery", "efficiency")
    max_disch_kw   = config.getfloat("battery", "max_discharge_kw")
    target_soc     = STATIC_TARGET_SOC
    critical_soc   = config.getfloat("control", "critical_soc")

    df = df_raw.copy()
    current_soc = float(initial_soc)
    n = len(df)

    def ev_load(month, weekday, hour):
        v = consumption_lookup.get((month, weekday, hour))
        if v is None:
            v = consumption_lookup.get((weekday, hour))
        return float(v or 0.0)

    cols = ["soll_soc", "critical_soc", "ist_soc", "soc_bedarf",
            "command", "grid_charge_kwh", "pv_charge_kwh", "forecast_demand_kwh"]
    result = {c: [] for c in cols}

    for idx in range(n):
        hour    = int(df.loc[idx, "hour"])
        month   = int(df.loc[idx, "month"]) if "month" in df.columns else None
        weekday = int(df.loc[idx, "weekday"])

        pv_kw      = float(df.loc[idx, "pv_generation_kw"] or 0.0)
        mkt_load   = float(df.loc[idx, "supermarket_consumption_kw"] or 0.0)
        discharge  = min(ev_load(month, weekday, hour), max_disch_kw)

        pv_surplus  = max(0.0, pv_kw - mkt_load)
        free_cap_kwh = max(0.0, (100.0 - current_soc) / 100.0 * capacity_kwh)
        pv_charge   = min(pv_surplus, max_charge_kw, free_cap_kwh)

        # Netzladung: nur wenn SOC unter Schwelle
        grid_charge = 0.0
        if current_soc < target_soc:
            command = "recharge"
            cur_energy  = (current_soc / 100) * capacity_kwh
            tgt_energy  = (target_soc  / 100) * capacity_kwh
            bat_needed  = max(0.0, tgt_energy - cur_energy + discharge / max(0.01, efficiency))
            grid_needed = max(0.0, bat_needed / max(0.01, efficiency) - pv_charge)
            grid_charge = min(max_charge_kw, grid_needed)
        else:
            command = "no_action"

        charged    = (pv_charge + grid_charge) * efficiency
        discharged = discharge / max(0.01, efficiency)
        soc_delta  = ((charged - discharged) / capacity_kwh) * 100.0
        current_soc = max(0.0, min(100.0, current_soc + soc_delta))

        result["soll_soc"].append(target_soc)
        result["critical_soc"].append(critical_soc)
        result["ist_soc"].append(current_soc)
        result["soc_bedarf"].append(0.0)
        result["command"].append(command)
        result["grid_charge_kwh"].append(grid_charge)
        result["pv_charge_kwh"].append(pv_charge)
        result["forecast_demand_kwh"].append(discharge)

    for col, vals in result.items():
        df[col] = vals
    return df


def run_year_simulation_static(
    config,
    prices_df: pd.DataFrame,
    pv_df: pd.DataFrame,
    consumption_lookup: dict,
    scenario_name: str = "Szenario_B_Statisch",
    initial_soc: float = 50.0,
) -> pd.DataFrame:
    """Jahreslauf mit statischer Schwellensteuerung (Referenzszenario)."""
    all_days   = []
    current_soc = float(initial_soc)
    year_dates  = pd.date_range(f"{YEAR}-01-01", f"{YEAR}-12-31", freq="D")

    print(f"\n  {'─'*56}")
    print(f"  Szenario: {scenario_name}")
    print(f"  Soll-SOC: {STATIC_TARGET_SOC:.0f}%  |  "
          f"Festpreis: {STATIC_PRICE_CT_KWH:.0f} ct/kWh  |  "
          f"Start-SOC: {current_soc:.1f}%")
    print(f"  {'─'*56}")

    for i, ts in enumerate(year_dates):
        day = ts.date()
        if i % 30 == 0:
            print(f"    Tag {i+1:3d}/365: {day}  |  SOC: {current_soc:5.1f}%")

        day_raw = build_day_df(day, prices_df, pv_df, config)
        # Festpreis ueberschreibt dynamische Preisberechnung fuer Szenario B
        day_raw["price_ct_kwh"] = STATIC_PRICE_CT_KWH
        day_sim = _simulate_day_static(day_raw, consumption_lookup, initial_soc=current_soc, config=config)

        current_soc = float(day_sim["ist_soc"].iloc[-1])
        day_sim["date"]     = str(day)
        day_sim["scenario"] = scenario_name
        all_days.append(day_sim)

    print(f"    End-SOC: {current_soc:.1f}%")
    return pd.concat(all_days, ignore_index=True)


# ── KPI-Berechnung ────────────────────────────────────────────────────────────

def compute_kpis(results_df: pd.DataFrame) -> dict:
    """Berechnet jaehrliche Kennzahlen aus dem Stunden-Ergebnis-DataFrame."""
    pv_hours      = int((results_df["pv_generation_kw"] > 0).sum())
    pv_self_kwh   = float(results_df["pv_charge_kwh"].sum())
    grid_kwh      = float(results_df["grid_charge_kwh"].sum())
    cost_eur      = float((results_df["grid_charge_kwh"] * results_df["price_ct_kwh"] / 100).sum())
    demand_kwh    = float(results_df["forecast_demand_kwh"].sum())
    recharges     = int((results_df["command"] == "recharge").sum())
    defers        = int((results_df["command"] == "defer_charge").sum())
    avg_price     = (cost_eur / grid_kwh * 100) if grid_kwh > 0 else 0.0

    return {
        "PV-Stunden gesamt":         pv_hours,
        "PV-Eigennutzung [kWh]":     round(pv_self_kwh,  1),
        "EV-Gesamtbedarf [kWh]":     round(demand_kwh,   1),
        "Netzbezug [kWh]":           round(grid_kwh,     1),
        "Netzbezug Kosten [EUR]":    round(cost_eur,     2),
        "Durchschn. Bezugspreis [ct/kWh]": round(avg_price, 2),
        "Nachlade-Ereignisse":       recharges,
        "Verzoegerte Ladungen":      defers,
    }


def monthly_kpis(results_df: pd.DataFrame) -> pd.DataFrame:
    """Monatliche Aufschluesselung der Kennzahlen."""
    df = results_df.copy()
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["monat"]      = df["start_time"].dt.to_period("M")
    df["kosten_eur"] = df["grid_charge_kwh"] * df["price_ct_kwh"] / 100

    return df.groupby("monat").agg(
        pv_stunden           =("pv_generation_kw",    lambda x: (x > 0).sum()),
        pv_eigennutzung_kwh  =("pv_charge_kwh",        "sum"),
        ev_bedarf_kwh        =("forecast_demand_kwh",  "sum"),
        netzbezug_kwh        =("grid_charge_kwh",       "sum"),
        kosten_eur           =("kosten_eur",            "sum"),
        recharge_count       =("command",               lambda x: (x == "recharge").sum()),
        defer_count          =("command",               lambda x: (x == "defer_charge").sum()),
    ).round(2)


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  Jahressimulation EMS – PV-gepufferte HPC-Ladestation")
    print(f"  Simulationsjahr: {YEAR}")
    print("=" * 60)

    config      = load_settings()
    initial_soc = config.getfloat("general", "default_fallback_soc")

    # 1 – Lastprofil laden
    print("\n[1/4] Lade EV-Lastprofil …")
    consumption_lookup = load_historical_profile_data(config)
    print(f"  {len(consumption_lookup)} Zeitschluessel geladen.")

    # 2 – Jahrespreise laden
    print("\n[2/4] Lade Jahrespreise …")
    prices_df = load_year_prices(config)
    print(f"  {len(prices_df)} Preisstunden: "
          f"{prices_df['start_time'].min().date()} – "
          f"{prices_df['start_time'].max().date()}")

    # 3 – PV-Jahresdaten laden (Open-Meteo Archiv-API, gecacht)
    print("\n[3/4] Lade PV-Jahresdaten (Open-Meteo Archiv) …")
    pv_df = fetch_pv_year(config, year=YEAR)
    pv_total_hours = int((pv_df["pv_generation_kw"] > 0).sum())
    print(f"  {pv_total_hours} Stunden mit PV-Einstrahlung > 0 kW")

    # 4 – Simulation beider Szenarien
    print("\n[4/4] Starte Simulationslaeufe …")

    df_a = run_year_simulation_dynamic(
        config, prices_df, pv_df, consumption_lookup,
        scenario_name="Szenario_A_Dynamisch",
        initial_soc=initial_soc,
    )

    df_b = run_year_simulation_static(
        config, prices_df, pv_df, consumption_lookup,
        scenario_name="Szenario_B_Statisch",
        initial_soc=initial_soc,
    )

    # ── KPI-Ausgabe ────────────────────────────────────────────────────────
    kpi_a = compute_kpis(df_a)
    kpi_b = compute_kpis(df_b)

    print("\n" + "=" * 70)
    print("  ERGEBNISSE – Jahressimulation 2025")
    print("=" * 70)
    hdr = f"  {'KPI':<38} {'Sz. A (Dyn.)':>12} {'Sz. B (Stat.)':>13}"
    print(hdr)
    print(f"  {'-'*66}")
    for key in kpi_a:
        va = kpi_a[key]
        vb = kpi_b[key]
        if isinstance(va, float):
            print(f"  {key:<38} {va:>12.2f} {vb:>13.2f}")
        else:
            print(f"  {key:<38} {va:>12d} {vb:>13d}")

    diff = kpi_a["Netzbezug Kosten [EUR]"] - kpi_b["Netzbezug Kosten [EUR]"]
    diff_pct = (diff / kpi_b["Netzbezug Kosten [EUR]"] * 100) if kpi_b["Netzbezug Kosten [EUR]"] > 0 else 0
    print(f"\n  Kostendifferenz A – B : {diff:+.2f} EUR  ({diff_pct:+.1f} %)")
    print(f"  (negativ = Szenario A guenstiger als Szenario B)")

    # ── Monatliche Auswertung ──────────────────────────────────────────────
    monthly_a = monthly_kpis(df_a)
    monthly_b = monthly_kpis(df_b)

    print("\n  Monatliche Uebersicht – Szenario A:")
    print(monthly_a.to_string())
    print("\n  Monatliche Uebersicht – Szenario B:")
    print(monthly_b.to_string())

    # ── Dateien speichern ──────────────────────────────────────────────────
    out = {
        "ergebnis_szenario_a.csv":  df_a,
        "ergebnis_szenario_b.csv":  df_b,
    }
    for fname, df in out.items():
        path = os.path.join(BASE_DIR, fname)
        df.to_csv(path, index=False, encoding="utf-8-sig")

    monthly_a.to_csv(os.path.join(BASE_DIR, "monatlich_szenario_a.csv"), encoding="utf-8-sig")
    monthly_b.to_csv(os.path.join(BASE_DIR, "monatlich_szenario_b.csv"), encoding="utf-8-sig")

    kpi_df = pd.DataFrame([kpi_a, kpi_b], index=["Szenario_A_Dynamisch", "Szenario_B_Statisch"])
    kpi_df.to_csv(os.path.join(BASE_DIR, "kpi_jahresvergleich.csv"), encoding="utf-8-sig")

    print("\n  Gespeicherte Dateien:")
    print("    ergebnis_szenario_a.csv    – Stundenergebnisse Szenario A (8760 Zeilen)")
    print("    ergebnis_szenario_b.csv    – Stundenergebnisse Szenario B (8760 Zeilen)")
    print("    monatlich_szenario_a.csv   – Monatliche KPIs Szenario A")
    print("    monatlich_szenario_b.csv   – Monatliche KPIs Szenario B")
    print("    kpi_jahresvergleich.csv    – KPI-Vergleich A vs. B")
    print(f"    pv_{YEAR}_cache.csv         – PV-Jahresdaten (Cache fuer Folgelaeufe)")
    print()


if __name__ == "__main__":
    main()
