# Project Context: Polymarket BTC 5m Trading Bot (Simulation Phase)

## 1. Project Overview
High-frequency simulation bot designed to trade "Bitcoin Up/Down" 5-minute binary markets on Polymarket. The goal is to identify high-probability entries at price levels near $0.95$ and optimize the risk/reward ratio using order book microstructure analysis.

## 2. Technical Stack
- **Language:** Python 3.10+ (Asyncio)
- **APIs:** - **Gamma API (REST):** For market discovery and metadata (slugs, token IDs).
    - **CLOB WebSocket:** For real-time Order Book updates (Level 2 data).
- **Database:** SQLite (tracking simulated trades and performance).
- **Environment:** Designed for low-latency execution and real-time monitoring.

## 3. Current Implementation Status
- **Connectivity:** WebSocket logic is stable. Implemented a heartbeat (Ping) to prevent connection silent-freezes.
- **Slug Logic:** Fixed. Slugs use the **start timestamp** of the 5-minute window (e.g., `btc-updown-5m-1709689500`).
- **Data Parsing:** Updated for 2026 message structures. Handles `price_changes` and `book` snapshot events.
- **Performance:** Currently showing a ~92% hit ratio on raw $0.95$ entries, but with **negative expectancy** (losing money overall) due to the 19:1 risk/reward ratio.
