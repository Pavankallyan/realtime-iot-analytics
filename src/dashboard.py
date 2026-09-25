"""Live IoT fleet dashboard — CloudWatch-style monitoring UI.

Reads the SQLite store written by the stream processor and auto-refreshes.
Run:
    streamlit run src/dashboard.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.models import FLEET, METRIC_META

st.set_page_config(page_title="IoT Fleet Monitor", layout="wide")
st.title("📡 IoT Fleet Monitor")
st.caption("Live smart-home sensor telemetry · simulated stream → rolling analytics")


# -- data access -------------------------------------------------------------
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    if not config.DB_PATH.exists():
        return pd.DataFrame()
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        return pd.read_sql(sql, con, params=params)
    finally:
        con.close()


def load_recent(device_id: str, minutes: int) -> pd.DataFrame:
    df = query(
        """SELECT ts, temperature_c, humidity_pct, power_w, vibration_g, is_anomaly
           FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT 3000""",
        (device_id,))
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return df[df["ts"] >= cutoff].sort_values("ts")


# -- sidebar ------------------------------------------------------------------
st.sidebar.header("Controls")
live = st.sidebar.checkbox("Live refresh", value=True)
minutes = st.sidebar.slider("Chart window (minutes)", 2, 60,
                            config.DEFAULT_CHART_MINUTES)
device_ids = [d.device_id for d in FLEET]
selected = st.sidebar.selectbox("Device", device_ids)
st.sidebar.caption("Start the pipeline first:\n"
                   "`python -m src.simulator` → `python -m src.processor`")

# -- fleet KPIs -----------------------------------------------------------------
fleet = query("SELECT * FROM fleet_stats ORDER BY ts DESC LIMIT 1")
readings_now = query("SELECT COUNT(*) AS n FROM readings")
if fleet.empty:
    st.info("⏳ Waiting for data — start the simulator and processor, "
            "then keep this page open.")
    st.stop()

kpi = st.columns(4)
kpi[0].metric("Active devices",
              f"{fleet['active_devices'][0]} / {len(FLEET)}")
kpi[1].metric("Fleet power",
              f"{fleet['total_power_w'][0]:,.0f} W" if fleet['total_power_w'][0] else "—")
kpi[2].metric("Avg temperature",
              f"{fleet['avg_temp_c'][0]:.1f} °C" if fleet['avg_temp_c'][0] else "—")
alerts_5 = query("SELECT COUNT(*) AS n FROM alerts "
                 "WHERE ts >= datetime('now', '-5 minutes')")["n"][0]
kpi[3].metric("Alerts (5 min)", alerts_5,
              delta="attention needed" if alerts_5 else "all clear",
              delta_color="inverse" if alerts_5 else "normal")

# -- device detail ---------------------------------------------------------------
st.subheader(f"Device: {selected}")


def gauge_range(metric: str, df: pd.DataFrame) -> list:
    """Sensible gauge bounds per metric from observed data."""
    lo, hi = df[metric].min(), df[metric].max()
    pad = max((hi - lo) * 0.2, 1e-6)
    return [max(0, lo - pad) if metric != "temperature_c" else lo - pad,
            hi + pad]


df = load_recent(selected, minutes)
profile = next(d for d in FLEET if d.device_id == selected)

if df.empty:
    st.warning("No readings for this device in the selected window yet.")
else:
    metrics = [m for m in profile.metrics]
    # gauges for latest values
    latest = df.iloc[-1]
    gcols = st.columns(len(metrics))
    for col, m in zip(gcols, metrics):
        meta = METRIC_META[m]
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=latest[m],
            title={"text": f"{meta['label']} ({meta['unit']})"},
            gauge={"axis": {"range": gauge_range(m, df)},
                   "bar": {"color": "#4da3ff"}}))
        fig.update_layout(height=220, margin=dict(t=40, b=10, l=10, r=10))
        col.plotly_chart(fig, use_container_width=True)

    # time series with anomaly markers
    fig = make_subplots(rows=len(metrics), cols=1, shared_xaxes=True,
                        subplot_titles=[METRIC_META[m]["label"] for m in metrics])
    for i, m in enumerate(metrics, start=1):
        fig.add_trace(go.Scatter(x=df["ts"], y=df[m], mode="lines",
                                 name=METRIC_META[m]["label"],
                                 line=dict(color="#4da3ff", width=1.5)),
                      row=i, col=1)
        anom = df[df["is_anomaly"] == 1]
        if not anom.empty:
            fig.add_trace(go.Scatter(x=anom["ts"], y=anom[m], mode="markers",
                                     name="anomaly", marker=dict(color="#ff4d4d",
                                                                size=8, symbol="x"),
                                     showlegend=(i == 1)),
                          row=i, col=1)
    fig.update_layout(height=220 * len(metrics), margin=dict(t=40, b=20),
                      showlegend=True)
    st.plotly_chart(fig, use_container_width=True)


# -- fleet overview ----------------------------------------------------------------
st.subheader("Fleet overview")
fhist = query("SELECT ts, total_power_w, avg_temp_c, active_devices FROM fleet_stats "
              "ORDER BY ts DESC LIMIT 600")
if not fhist.empty:
    fhist["ts"] = pd.to_datetime(fhist["ts"], utc=True)
    fhist = fhist.sort_values("ts")
    c1, c2 = st.columns(2)
    with c1:
        fig = go.Figure(go.Scatter(x=fhist["ts"], y=fhist["total_power_w"],
                                   mode="lines", fill="tozeroy",
                                   line=dict(color="#ffb84d")))
        fig.update_layout(title="Total fleet power (W)", height=280,
                          margin=dict(t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        fig = go.Figure(go.Scatter(x=fhist["ts"], y=fhist["avg_temp_c"],
                                   mode="lines", line=dict(color="#4dd2ff")))
        fig.update_layout(title="Fleet avg temperature (°C)", height=280,
                          margin=dict(t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

# -- alerts + health -----------------------------------------------------------------
c1, c2 = st.columns([3, 2])
with c1:
    st.subheader("🚨 Alert feed")
    alerts = query("SELECT ts, device_id, kind, message FROM alerts "
                   "ORDER BY ts DESC LIMIT 25")
    if alerts.empty:
        st.success("No alerts — fleet nominal.")
    else:
        st.dataframe(alerts, use_container_width=True, hide_index=True)
with c2:
    st.subheader("Device health")
    rows = []
    for d in FLEET:
        last = query("SELECT ts FROM readings WHERE device_id = ? "
                     "ORDER BY ts DESC LIMIT 1", (d.device_id,))
        if last.empty:
            rows.append({"device": d.device_id, "status": "⚪ no data",
                         "last seen": "—"})
            continue
        age = (datetime.now(timezone.utc)
               - pd.to_datetime(last["ts"][0], utc=True)).total_seconds()
        recent_alert = query(
            "SELECT 1 FROM alerts WHERE device_id = ? AND ts >= datetime('now', '-5 minutes') LIMIT 1",
            (d.device_id,))
        if age > config.GAP_MULTIPLIER * config.TICK_INTERVAL_SEC:
            status = "🔴 offline"
        elif not recent_alert.empty:
            status = "🟡 anomaly"
        else:
            status = "🟢 ok"
        rows.append({"device": d.device_id, "status": status,
                     "last seen": f"{age:.0f}s ago"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption(f"Readings stored: {readings_now['n'][0]:,} · "
           f"DB: {config.DB_PATH.name} · simulated stream (see README for the "
           f"Kafka/AWS production mapping)")

# -- auto-refresh ----------------------------------------------------------------------
if live:
    time.sleep(config.DASHBOARD_REFRESH_SEC)
    st.rerun()
