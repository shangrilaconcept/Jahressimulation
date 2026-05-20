### Kernlogik EMS-Tool für dynamisches Steuerungskonzept einer PV-gepufferten HPC-Ladestation 
# Notwendigen Bibliotheken importieren, python ist lokal auf dem PC installiert (Grundvorraussetzung Python 3.14.3)

import configparser #Notwendig für einlesen der Konfigurationsdatei settings.ini
import os #Bib für Betriebssystem (Pfade etc.)
from datetime import datetime, timedelta #Bib für Timestamps und allgemeine Zeitberechnungen
from click import command
import openmeteo_requests #Bib für API-Client der Open Meteo API (PV-Prognose)
import pandas as pd #Bib für Dateaframes (Bearbeitung von Datensätzen) 
import pytz #Bib für Zeitzonen 
import requests #Bib für API-Requests (HTTP-Anfragen aus API Schnittstellen)
import requests_cache #Bib für Cache von API-Requests (spätere gleiche Abfragen werden aus dem Cache gezogen nicht live neu angefragt) 
from retry_requests import retry ##Bib für Wiederholung des API-Calls bei Fehler

BASE_DIR = os.path.dirname(os.path.abspath(__file__)) #Hauptverzeichnis

### Einlesen der Konfigurationsdatei settings.ini

def load_settings(settings_path=None):
    config = configparser.ConfigParser()
    config.optionxform = str  #1zu1 Übernahme der Paramater aus settings.ini
    target_path = settings_path or os.path.join(BASE_DIR, 'settings.ini')
    if not config.read(target_path, encoding="utf-8"):
        raise FileNotFoundError(f"Konfigurationsdatei konnte nicht gefunden werden: {target_path}")  #Fehlerfall
    return config #Erfolgsfall

def resolve_data_path(config, key):
    file_name = config.get("files", key)
    if os.path.isabs(file_name):
        return file_name
    return os.path.join(BASE_DIR, file_name)

### Supermarktverbrauch und dynamischer Tarif  
def get_supermarket_consumption_kw(hour, config):
    if 0 <= hour < 6:
        return config.getfloat("site_load", "night_kw")
    if 6 <= hour < 21:
        return config.getfloat("site_load", "day_kw")
    return config.getfloat("site_load", "evening_kw")


### Einlesen des dynamischen Netzentgelts aus Konfigurationsdatei, Abhängig von Tarifszeitfenster, 
def get_dynamic_netzentgelt_ct_kwh(hour, config):

    basis = config.getfloat("pricing", "p_netz_basis_ct_kwh")
    nt_start = config.getint("pricing_windows", "nt_start_hour")
    nt_end = config.getint("pricing_windows", "nt_end_hour")
    ht_start = config.getint("pricing_windows", "ht_start_hour")
    ht_end = config.getint("pricing_windows", "ht_end_hour")


    if nt_start <= hour < nt_end: 
        return basis * config.getfloat("pricing_windows", "nt_factor")
    if ht_start <= hour < ht_end: 
        return basis * config.getfloat("pricing_windows", "ht_factor")
    return basis * config.getfloat ("pricing_windows", "st_factor")

### Definition des Preisgesteuerten Soll-SOCs:

def calculate_price_soll_soc(price_ct_kwh):
    if price_ct_kwh < 0:
        return 100.0
    if price_ct_kwh < 20:
        return 60.0
    if price_ct_kwh < 28:
        return 50.0
    return None 

### Einlesen des Lastprofils der Station aus CSV-Datei Test #an sich nicht notwendig wenn csv in utf-8 angelegt ist

def load_historical_profile_data(config):
    csv_path = resolve_data_path(config, "historical_profile_file")
    df_hist = pd.read_csv(csv_path, sep=None, engine="python", encoding="latin-1")
    if len(df_hist.columns) == 1:
        for delim in [",", ";", "\t"]:
            candidate = pd.read_csv(csv_path, sep=delim, engine="python", encoding="latin-1")
            if len(candidate.columns) > 1:
                df_hist = candidate
                break

    df_hist.columns = [str(column).strip().lower() for column in df_hist.columns]
    df_hist = df_hist.rename(
        columns={
            "monat": "month",
            "wochentag_nr": "weekday",
            "stunde": "hour", 
            "verbrauch_kwh_mittel": "consumption_kwh",
            "verbrauch_kwh": "consumption_kwh",
            "consumption_wahrscheinlichkeit_kwh": "consumption_kwh",

        }
    )

    df_hist["hour"] =  pd.to_numeric(df_hist["hour"], errors="coerce")
    df_hist["consumption_kwh"] = pd.to_numeric(
        df_hist["consumption_kwh"].astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )
    df_hist = df_hist.dropna(subset=["hour", "consumption_kwh"])
    df_hist["hour"] = df_hist["hour"].astype(int)

    has_weekday = "weekday" in df_hist.columns
    has_month = "month" in df_hist.columns

    ###Sicherheit, dass falls eine falsch formatierte CSV Datei mit Wochentagsnr 1-7 eingelesen wird #Korrektur auf 0-6

    if has_weekday:
        df_hist["weekday"] = pd.to_numeric(df_hist["weekday"], errors="coerce")
        df_hist = df_hist.dropna(subset=["weekday"])
        df_hist["weekday"] = df_hist["weekday"].astype(int)
        if df_hist["weekday"].between(1, 7).all():
            df_hist["weekday"] = df_hist["weekday"] - 1 

    if has_month:
        df_hist["month"] = pd.to_numeric(df_hist["month"], errors="coerce")
        df_hist = df_hist.dropna(subset=["month"])
        df_hist["month"] = df_hist["month"].astype(int)

    if has_weekday and has_month:
        return df_hist.groupby(["month", "weekday", "hour"])["consumption_kwh"].mean().to_dict()
    if has_weekday:
        return df_hist.groupby(["weekday", "hour"])["consumption_kwh"].mean().to_dict()

    raise KeyError("Historical profile file needs at least weekday and hour columns.")

    
### Einlesen der PV-Daten aus Konfigurationsdatei

class PVForecastClient:
    def __init__(self):
        cache_session = requests_cache.CachedSession(os.path.join(BASE_DIR, ".cache_main"), expire_after=3600)
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        self.client = openmeteo_requests.Client(session=retry_session)

    def get_pv_forecast(self, start_dt_berlin, end_dt_berlin, config):
        url = config.get("api", "open_meteo_url")
        lat = config.getfloat("pv", "lat")
        lon = config.getfloat("pv", "lon")
        installed_kwp = config.getfloat("pv", "installed_kwp")
        pr_factor = config.getfloat("pv", "performance_ratio")

        params_base = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ["global_tilted_irradiance"],
            "models": "best_match",
            "timezone": config.get("general", "timezone"),
            "start_date": start_dt_berlin.strftime("%Y-%m-%d"),
            "end_date": (end_dt_berlin - timedelta(hours=1)).strftime("%Y-%m-%d"),
        }
        ###Auslesen der global_tilted_irradiance für beide Ausrichtungen (Ost und West) basierend auf dem Azimuthwinkel 
        resp_ost = self.client.weather_api(url, params={**params_base, "tilt": 15, "azimuth": 88})[0]
        resp_west = self.client.weather_api(url, params={**params_base, "tilt": 15, "azimuth": -92})[0]

        hourly = resp_ost.Hourly()
        gti_ost = hourly.Variables(0).ValuesAsNumpy()
        gti_west = resp_west.Hourly().Variables(0).ValuesAsNumpy()

        time_range = pd.date_range(
            start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
            end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
            freq=pd.Timedelta(seconds=hourly.Interval()),
            inclusive="left",
        ).tz_convert(config.get("general", "timezone")).tz_localize(None)
        
        mask = (
            (time_range >= start_dt_berlin.replace(tzinfo=None))
            & (time_range < end_dt_berlin.replace(tzinfo=None))
        )
        ### Berechung der Gesamtleistung der PV-Anlage basierend auf gti_ost und gti_west und der Performance Ratio der PV-Module
        half_kwp = installed_kwp / 2
        pv_kw = ((gti_ost[mask]/1000)*half_kwp *pr_factor) + ((gti_west[mask]/1000)*half_kwp * pr_factor)
        return pd.DataFrame({"start_time" : time_range[mask], "pv_generation_kw": pv_kw})
    
###Preisprognose Implemementierung mittels API-Call (awattar API), Transformation der Zeitstempel in lesbare Format mithilfe der KI (GPT.5.4 & Claude Sonnet 4.6)
def load_forecast_data_for_day(day_offset, config): 
    berlin_tz = pytz.timezone(config.get("general", "timezone"))
    now_utc = datetime.now(pytz.utc)
    target_date = (now_utc + timedelta(days=int(day_offset))).date()
    start_dt_berlin = berlin_tz.localize(datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0))
    next_day = target_date + timedelta(days=1)
    end_dt_berlin = berlin_tz.localize(datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0))

    start_ts = int(start_dt_berlin.astimezone(pytz.utc).timestamp()*1000)
    end_ts = int(end_dt_berlin.astimezone(pytz.utc).timestamp()*1000)

    response = requests.get( f"{config.get('api', 'awattar_market_url')}?start={start_ts}&end={end_ts}",
                            timeout=10,)
    response.raise_for_status()
    payload = response.json()

    if payload.get("data"):
        df_prices = pd.DataFrame(payload["data"])
        df_prices["start_time"] = pd.to_datetime(df_prices["start_timestamp"], unit="ms", utc=True).dt.tz_convert(config.get("general", "timezone")).dt.tz_localize(None)
        df_prices["marketprice_ct_per_kwh"] = df_prices["marketprice"] * 0.1 #Umrechnung von €/Mwh in ct/kwh
### Sicherung, dass wenn der API-Call fehlschlägt bzw. keine Daten zurückgibt, ein Dummy-Dataframe mit konstanten Nettoarbeitspreis von 25 ct/kwh für 24 Stunden des Tages gilt
    else:
        start_dummy = start_dt_berlin.replace(tzinfo=None)
        time_range_dummy = pd.date_range(start=start_dummy, periods=24, freq="h")
        df_prices = pd.DataFrame({ "start_time": time_range_dummy,
                                    "marketprice_ct_per_kwh": [25.0] * 24,})
  
  ### Berechnung des Bruttoarbeitspreises pro kwh basierend auf dem Marktpreis zu der Stunde, dem dynamischen Netzentgelt und den fixen Kosten (Steuer, Umlage, Vertrieb, MwSt)     
    df = df_prices[["start_time", "marketprice_ct_per_kwh"]].rename(columns={"marketprice_ct_per_kwh": "price_ct_kwh"})
    df["hour"] = df["start_time"].dt.hour
    df["netzentgelt_ct_kwh"] = df["hour"].apply(lambda hour: get_dynamic_netzentgelt_ct_kwh(hour, config))
    df["price_ct_kwh"] = (
        df["price_ct_kwh"]
        + df["netzentgelt_ct_kwh"]
        + config.getfloat("pricing", "p_stromsteuer_ct_kwh")
        + config.getfloat("pricing", "p_umlagen_ct_kwh")
        + config.getfloat("pricing", "p_vertrieb_ct_kwh")
    ) * config.getfloat("pricing", "mwst_factor")

### Erstellung eines Clients für die PV-Erzeugungsprognose und der Supermarktlast pro Stunde
### Beide Daten werden in den DataFrame eingelesen
    pv_df = PVForecastClient().get_pv_forecast(start_dt_berlin, end_dt_berlin, config)
    df = pd.merge(df, pv_df [["start_time", "pv_generation_kw"]], on="start_time", how="left")  

    df["supermarket_consumption_kw"] = df["start_time"].dt.hour.apply(lambda hour: get_supermarket_consumption_kw(hour, config))
    df["month"] = df["start_time"].dt.month
    df["weekday"] = df["start_time"].dt.dayofweek
    df["hour"] = df["start_time"].dt.hour
    return df.reset_index(drop=True)
###Einlesen der Steuerungsparameter aus der Konfigurationsdatei
def run_simulation (df_raw, consumption_lookup, initial_soc, config, lookahead_hours=None):
    battery_capacity_kwh = config.getfloat("battery", "capacity_kwh")
    max_charge_kw = config.getfloat("battery", "max_charge_kw")
    max_discharge_kw = config.getfloat("battery", "max_discharge_kw")
    battery_efficiency = config.getfloat("battery", "efficiency")
    critical_soc = 30.0
    critical_peak_soc = config.getfloat("control", "critical_peak_soc")
    peak_start_hour = config.getint("control", "peak_start_hour")
    peak_end_hour = config.getint("control", "peak_end_hour")
    soc_bedarf_cap_percent = config.getfloat("control", "soc_bedarf_cap_percent")  
    soc_bedarf_lookahead = lookahead_hours if lookahead_hours is not None else config.getint ("control", "soc_bedarf_lookahead_hours")
    price_lookahead_hours = config.getint("control", "price_lookahead_hours")

### Vorschlag der KI (GPT-5.4 & Claude Sonnet 4.6) eine Kopie der DataFrames zu nutzen, damit die ursprünglichen Daten nicht verändert werden 
### und die Simulation auf einer Sauberen Kopie läuft (für spätere Schleifen und Berechnungen)
    df = df_raw.copy()
    current_soc = float(initial_soc) ## hier wird der uebergebene Start-SOC (initial_soc) als aktueller SOC gesetzt
    n = len(df) ### Durchlaufzahl n wird auf die Zeilen des DataFrames gesetzt (n=24 Stunden)

### Hier wird der Schlüssel des jeweiligen Zeitslots herausgesucht
    def lookup_ev_load(month, weekday, hour): 
        value = consumption_lookup.get((month, weekday, hour))
        if value is None:  ### Sicherheit: Falls kein passender Schlüssel gefunden wurde, nutzt der Algorithmus den Fallback aus Wochentag + Stunde
            value = consumption_lookup.get((weekday, hour)) 
        return float(value or 0.0) ### Falls kein Wert gefunden wurde, wird 0.0 zurückgegeben (kein Ladebedarf in der Stunde)
    
### Hier wird der Bedarf pro Stunde berechnet, indem der PV-Überschuss (pv_surplus) aus PV-erzeugung (pv_kw) und Supermarktverbrauch (market load) gebildet wird
### Anschließend wird aus dem PV-Überschuss und EV-Bedarf (ev_load) die Nettolast ((net_demand_per_hour) berechnet,
### wieviel Leistung Übrig Bleibt (PV > EV-Bedarf) oder wieviel Leistung zusätzlich benötigt wird (EV-Bedarf > PV-Überschuss)))
    net_demand_per_hour = []
    for index in range(n): 
        pv_kw = float(df.loc[index, "pv_generation_kw"] or 0.0)
        market_load = float(df.loc[index, "supermarket_consumption_kw"] or 0.0)
        month = int(df.loc[index, "month"]) if "month" in df.columns else None
        weekday = int(df.loc[index, "weekday"])
        hour = int(df.loc[index, "hour"])
        ev_load = lookup_ev_load(month, weekday, hour)
        pv_surplus = max(0.0, pv_kw - market_load)
        net_demand_per_hour.append(max(0.0, ev_load - pv_surplus)) # Falls PV > EV-Bedarf dann wird der Netto-Bedarf = 0

### Berechnung des SOC-Bedarfs in den nöchsten Stunden
    _critical_soc_base = config.getfloat("control", "critical_soc")  # Puffer: SOC soll nie unter critical_soc fallen
    soc_bedarf_forward = []
    for index in range(n): 
        horizon_end = min(n, index + soc_bedarf_lookahead) ###Horizont Integration mit Hilfe der KI (GPT-5.4 & Claude Sonnet 4.6) horizon_end = Ende des lookahead-Fensters
        remaining_kwh = sum(net_demand_per_hour [index:horizon_end]) ##Summe des Restbedarfs der nächsten Stunden (Wie hoch muss der SOC sein um den Restbedarf zu decken?)
        # critical_soc wird als Puffer addiert: der SOC muss immer mindestens critical_soc + Restbedarf betragen
        # so wird proaktiv bei günstigen Preisen geladen, bevor der critical_soc überhaupt erreicht wird
        soc_bedarf_forward.append(min(soc_bedarf_cap_percent, (remaining_kwh/battery_capacity_kwh)*100 + _critical_soc_base)) # SOC-Bedarf [%] = (benötigte kwH / 201 kwh) *100) + critical_soc Puffer, maximal aber 90%

### Prüfung, ob in den nächsten Stunden ein günstigerer Preis zu erwarten ist, um zu gucken ob jetzt nachgeladen werden soll
### oder ob in einem späteren Zeitslot günstigere Preise zu erwarten sind
    def has_cheaper_future_price (start_idx, current_price):
        last_idx = min(start_idx + price_lookahead_hours + 1, n) # price_lookahead_hours = 24 Stunden aus der Config
        if start_idx + 1 >=last_idx: #Sicherheit am Ende des Zeitfensters, dass nicht weiterezählt wird als die Anzahl der Zeilen im DataFrame
            return False
        future_prices = df.loc[start_idx + 1: last_idx -1, "price_ct_kwh"] ### Integration mittels KI (GPT-5.4 & Claude Sonnet 4.6)
        return (not future_prices.empty) and float (future_prices.min()) < float(current_price) # prüft future_prices und wählt den günstigsten Preis --> future_price.min() < current_price = True, future_price.min() > current_price = False 
    ### True heißt: Später wird es günstiger, False heißt: Es wird nicht günstiger in den nächsten Stunden


    result_rows = {
        "soll_soc": [],
        "critical_soc": [],
        "ist_soc": [],
        "soc_bedarf": [],
        "command": [],
        "grid_charge_kwh": [],
        "pv_charge_kwh": [],
        "forecast_demand_kwh": [],
    }# Zeilennamen werden hinzugefügt

###Grenzbestimmungen für spätere Ladeentscheidungen
    for index in range(n):
        price = float(df.loc[index, "price_ct_kwh"])
        hour = int(df.loc[index, "hour"])
        month = int(df.loc[index, "month"]) if "month" in df.columns else None
        weekday = int(df.loc[index, "weekday"])

        pv_kw = float(df.loc[index, "pv_generation_kw"] or 0.0)
        market_load = float(df.loc[index, "supermarket_consumption_kw"] or 0.0)
        ev_load = lookup_ev_load(month, weekday, hour)
        discharge_kwh = min(ev_load, max_discharge_kw)
        pv_surplus_kw = max(0.0, pv_kw - market_load)
        pv_charge_kwh = min(pv_surplus_kw, max_charge_kw, max(0.0, (100.0 - current_soc) / 100.0 * battery_capacity_kwh),) #Berechnung wieviel freie Speicherkapazität in der Batterie ist
##########SOC-Bestimmung (soc_bedarf, critical_soc, price_soc)
        soc_bedarf = soc_bedarf_forward[index]
        critical_soc = config.getfloat("control", "critical_soc")
        if peak_start_hour <= hour < peak_end_hour:
            critical_soc = max(critical_soc, critical_peak_soc)    #Überschreibt den critical_soc in den Peakstunden auf critical_peak_soc

        price_target = calculate_price_soll_soc(price)
        soc_price = price_target if price_target is not None else 0.0
        soll_soc = max(critical_soc, soc_bedarf, soc_price)  ###Kernbestimmung des Soll-SOC zwischen kritischen soc als unterste Grenze und (preis soc und soc_bedarf) als obere Grenze 
#### Implementierung verletzunglogik und critical guard für den Fall einer "Notfall"-Nachladung 
        current_energy_kwh = (current_soc/100) *battery_capacity_kwh
        projected_soc_without_grid = max(
            0.0,
            min(
                100.0,
                current_soc + ((pv_charge_kwh * battery_efficiency - (discharge_kwh/max(0.01, battery_efficiency)))/battery_capacity_kwh)*100,
            ),
        )
        critical_guard_active = current_soc < critical_soc or projected_soc_without_grid < critical_soc
        soll_soc_violated = current_soc < soll_soc or projected_soc_without_grid < soll_soc
        has_pv_surplus = pv_surplus_kw > 0.0
        force_price_recharge = price <= 0

    ##############Entscheidungslogik für die Ladeentscheidung basierend auf den Grenzbestimmungen ###################################
        if critical_guard_active:
            command = "recharge" #Erzwungene Nachladung 
            target_soc = soll_soc if force_price_recharge else critical_soc #Erzwungene Nachladung wenn Preis < 0 auf soll_soc, ansonsten auf critical_soc
        elif force_price_recharge and soll_soc_violated: #Wenn der Preis negativ ist und der Soll-Soc verletzt wird = sofortige Nachladung
            command = "recharge" 
            target_soc = soll_soc #bis auf soll_soc nachladen
        elif current_soc < soc_bedarf and (price_target is not None or has_pv_surplus): #wenn aktueller soc kleiner als soc_bedarf und ein price_soc definiert ist oder ein PV-Überschuss vorhanden ist = nachladung
            if has_pv_surplus:
                command = "recharge" 
                target_soc = soc_bedarf
            elif has_cheaper_future_price(index, price): #Wenn in den nächsten Stunden günstigere Preise zu erwarten sind = Verzögertes Nachladen 
                command = "defer_charge"
                target_soc = soc_bedarf
            else:
                command = "recharge"
                target_soc = soc_bedarf
        elif soll_soc_violated:# Wenn der Soll-SOC verletzt wird, aber kein unmittelbarer Nachladezwang besteht, wird abhängig von PV-Überschuss oder zukünftigen Preisen entschieden
            if has_pv_surplus:  #wenn PV-Überschuss vorhanden ist = Nachladen bis 100%
                command = "recharge"
                target_soc = soll_soc
            elif has_cheaper_future_price(index, price): #wenn in den nächsten Stunden günstigere Preise zu erwarten sind = Verzögertes Nachladen bis Soll-SOC
                command = "defer_charge"
                target_soc = soll_soc
            else: 
                command ="recharge" #wenn keine günstigeren Preise zu erwarten sind = sofortiges Nachladen bis Soll-SOC
                target_soc = soll_soc
        else: 
            command = "no_action" #wenn keine der Bedingungen erfüllt ist, wird keine Ladung = no_action ausgeführt
            target_soc = current_soc

###############################################
# Berechnung grid_charge
        grid_charge_kwh = 0.0
        if command == "recharge":
            target_energy_kwh = (target_soc /100)*battery_capacity_kwh 
            required_battery_input_kwh = max(0.0, target_energy_kwh - current_energy_kwh + (discharge_kwh /max(0.01, battery_efficiency))) # Berechnung der benötigten Energie
            needed_grid_kwh = max(0.0, (required_battery_input_kwh / max(0.01, battery_efficiency)) - pv_charge_kwh) # Berechnung wieviel zusätzliche Energie aus dem Netz benötigt wird
            grid_charge_kwh = min(max_charge_kw, needed_grid_kwh) #Begrenzung zwischen max charge (86kw) und der benötigten Energie aus dem Netz

        charged_into_battery_kwh = (pv_charge_kwh + grid_charge_kwh) * battery_efficiency # Berechnung der tatsächlichen Ladung in die Batterie (PV + Netzladung)
        discharged_from_battery_kwh = discharge_kwh / max(0.01, battery_efficiency) 
        soc_change = ((charged_into_battery_kwh - discharged_from_battery_kwh) / battery_capacity_kwh) * 100.0 #Berechnung der SOC-Änderung basierend auf der Ladung und Entladung
        current_soc = max(0.0, min(100.0, current_soc + soc_change)) 

        result_rows["soll_soc"].append(soll_soc)
        result_rows["critical_soc"].append(critical_soc)
        result_rows["ist_soc"].append(current_soc)
        result_rows["soc_bedarf"].append(soc_bedarf)
        result_rows["command"].append(command)
        result_rows["grid_charge_kwh"].append(grid_charge_kwh)
        result_rows["pv_charge_kwh"].append(pv_charge_kwh)
        result_rows["forecast_demand_kwh"].append(discharge_kwh)

    for column, values in result_rows.items():
        df[column] = values
    return df 
### KI-Vorschlag (GPT-5.4 & Claude Sonnet 4.6) zur Anwendung der Live-Daten auf die Simulationsergebnisse des Tages, um die Steuerungsentscheidungen mit den aktuellen Echtzeitdaten zu aktualisieren und anzupassen
def apply_live_anchor_to_today (df_today, df_today_raw, consumption_lookup, live_soc, now_marker, config): #Simulationsergebnisse werden mit Echzeit Daten aktualisiert
    if live_soc is None or now_marker is None or df_today is None or df_today.empty:
        return df_today
    
    anchor_time = now_marker.replace(minute=0, second=0, microsecond=0) # aufrundung auf volle Stunde

    future_raw = df_today_raw[df_today_raw["start_time"] >= anchor_time].copy().reset_index(drop=True)  #Auswahl der Zeilen ab der aktuellen Stunde (anchor_time) für die zukünftigen Stunden des Tages
    if future_raw.empty: 
        return df_today #Sicherheit: Falls keine zukünftigen Zeilen vorhanden sind, wird der ursprüngliche DataFrame zurückgegeben
    
    future_sim = run_simulation(future_raw, consumption_lookup, initial_soc=float(live_soc), config=config) #Simulation mit den zukünftigen Stunden des Tages und dem aktuellen SOC als Start-SOC
    # run_simulation liefert bereits korrekte Steuerungsbefehle basierend auf live_soc als Startwert.
    # Eine nachträgliche Überschreibung der Befehle würde die korrekte Preis- und SOC-Bedarfslogik zerstören.

    merge_cols = ["soll_soc", "critical_soc", "ist_soc", "soc_bedarf", "grid_charge_kwh", "pv_charge_kwh", "forecast_demand_kwh", "command"]
    
    future_by_time = future_sim.set_index("start_time")
### Aktualisierung der Steuerungsentscheidungen im ursprünglichen DataFrame für die Stunden ab der aktuellen Stunde (anchor_time) mit den Ergebnissen der Simulation basierend auf dem Live-SOC
    result_df = df_today.copy()
    # Vergangene Stunden (vor anchor_time) auf no_action setzen, da keine Aktion mehr möglich ist
    result_df.loc[result_df["start_time"] < anchor_time, "command"] = "no_action"
    result_df.loc[result_df["start_time"] < anchor_time, "grid_charge_kwh"] = 0.0
    for column in merge_cols: 
        result_df.loc[result_df["start_time"] >= anchor_time, column] = result_df.loc[
            result_df["start_time"] >= anchor_time, "start_time"
        ].map(future_by_time[column])

### Minutengenaue Anpssung der Steuerungsentscheidung mit dem aktuellen live-SOC

    current_hour = now_marker.replace(minute=0, second=0, microsecond=0)
    current_mask = result_df ["start_time"] == current_hour #
    if current_mask.any(): #
        current_idx = result_df.index[current_mask][0] # 
        live_soc_value = float(live_soc) # 
        result_df.at[current_idx, "ist_soc"] = live_soc_value # 
# aktualisierung des ist-soc mit dem aktuellen live-soc
        critical_soc = float(result_df.at[current_idx, "critical_soc"] or 0.0) 
        soll_soc = float(result_df.at[current_idx, "soll_soc"])
        target_soc = max(critical_soc, soll_soc)

### Entscheidungsüberschreibuung basierend auf den Live-SoC
        if live_soc_value >= target_soc:
            result_df.at[current_idx, "grid_charge_kwh"] = 0.0
            result_df.at[current_idx, "command"] = "no_action"
        elif live_soc_value <= critical_soc: 

            battery_capacity_kwh = config.getfloat("battery", "capacity_kwh")
            max_charge_kw = config.getfloat("battery", "max_charge_kw")
            battery_efficiency = config.getfloat("battery", "efficiency")
            
            current_energy_kwh = (live_soc_value/100)* battery_capacity_kwh
            target_energy_kwh = (target_soc/100)* battery_capacity_kwh
            discharge_kwh = float(result_df.at[current_idx, "forecast_demand_kwh"] or 0.0)
            pv_charge_kwh = float(result_df.at[current_idx, "pv_charge_kwh"])

            required_battery_input_kwh = max(0.0, target_energy_kwh - current_energy_kwh + (discharge_kwh / max(0.01, battery_efficiency)))
            needed_grid_kwh = max(0.0, (required_battery_input_kwh / max(0.01, battery_efficiency)) - pv_charge_kwh)
            result_df.at[current_idx, "grid_charge_kwh"] = min(max_charge_kw, needed_grid_kwh)
            result_df.at[current_idx, "command"] = "recharge"
        else:
            result_df.at[current_idx, "grid_charge_kwh"] = 0.0
            result_df.at[current_idx, "command"] = "defer_charge"

    return result_df

###Prüfung ob ob ein neuer Cycle anfangen muss 

def is_intraday_minute_cycle_due(last_cycle_at, now_marker=None, cadence_seconds=60):
    #Prüft, ob seit dem letzten Intraday-Zyklus genug Zeit vergangen ist.
    #Die Funktion dient als Taktgeber

    #Aktuellen Zeitpunkt bestimmen, falls er ungültig ist, kein neuer Lauf
    now_ts = pd.to_datetime(now_marker or datetime.now())
    if pd.isna(now_ts):
        return False

    #Beim ersten Lauf sofort starten
    if last_cycle_at is None:
        return True 
    
    #Ungültigen Zeitstempel des letzten Laufs ebenfalls als sofort starten behandeln
    last_ts = pd.to_datetime(last_cycle_at)
    if pd.isna(last_ts):
        return True
    
    #Verstrichene Zeit mit dem Takt vergleichen
    elapsed_seconds = (now_ts - last_ts).total_seconds()
    return elapsed_seconds >= int(cadence_seconds)

### Hauptfunktion für den Intraday-Minutenzyklus, der die Steuerungsentscheidungen basierend auf den aktuellen Echtzeitdaten und der Simulation aktualisiert und anpasst

def run_intraday_minute_cycle(config, live_soc, now_marker=None, cadence_seconds=60, last_cycle_at=None):
    now_ts = pd.to_datetime(now_marker or datetime.now())
    if pd.isna(now_ts):
        now_ts = datetime.now()

    if not is_intraday_minute_cycle_due(last_cycle_at, now_marker=now_ts, cadence_seconds=cadence_seconds):
        return {"cycle_due": False,
                "cycle_timestamp": pd.to_datetime(last_cycle_at) if last_cycle_at is not None else now_ts,
                "active_command": None,
                "active_hour": None,
                "df_today": None,}
        
    consumption_lookup = load_historical_profile_data(config)
    df_today_raw = load_forecast_data_for_day(0, config)

    fallback_soc = config.getfloat("general", "default_fallback_soc")
    live_soc_value = float(live_soc) if live_soc is not None else float(fallback_soc)
    
    df_today =run_simulation(df_today_raw, consumption_lookup, initial_soc=live_soc_value, config=config)
    df_today = apply_live_anchor_to_today(df_today, df_today_raw,consumption_lookup,live_soc=live_soc_value,
                                            now_marker=now_ts.to_pydatetime() if hasattr(now_ts, "to_pydatetime") else now_ts,
                                            config=config,)


    active_hour= now_ts.replace(minute=0, second=0, microsecond=0)
    time_floor = pd.to_datetime(df_today["start_time"], errors="coerce").dt.floor("h")
    active_rows = df_today[time_floor == active_hour]
    active_command = str(active_rows.iloc[0]["command"]) if not active_rows.empty else None #aktive Steuerunsentscheidung basierend auf aktueller Stunde (active_hour) im DataFrame gesucht, falls gefunden wird die erste Zeile ausgewählt und die Spalte "command" als aktive Steuerungsentscheidung zurückgegeben, ansonsten None

    return {"cycle_due": True,
            "cycle_timestamp": now_ts.replace(second=0, microsecond=0),
            "active_command": active_command,
            "active_hour": active_hour,
            "df_today": df_today,}

def summarize_day(df):
    if df is None or df.empty:
        return {}
    return {"start_soc": float(df["ist_soc"].iloc[0]),
            "end_soc": float(df["ist_soc"].iloc[-1]),
            "grid_kwh": float(df["grid_charge_kwh"].sum()),
            "pv_kwh": float(df["pv_charge_kwh"].sum()),
            "demand_kwh": float(df["forecast_demand_kwh"].sum()),
            "recharge_count": int((df["command"] == "recharge").sum()),
            "defer_count": int((df["command"] == "defer_charge").sum()),
            
            }


    





















    

        
    

    
    


   



                                                        


                      

    
    





                               