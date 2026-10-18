# Strategie-Audit – Zahlen (automatisch erzeugt)

## 1. Risiko je Position, je Tag und im Portfolio (Trendfolge, 100 % investiert)

Trades 03/2016–10/2026: 72, davon Notfall-Stops: 20.

| Kennzahl | Median | schlechtester Fall |
|---|---|---|
| Kurslücke Stop → Eröffnung der Auslöse-Stunde | +0.00 % | +0.00 % |
| Stop → Tief der Auslöse-Stunde (pessimistisch) | -1.36 % | -16.32 % |
| Ergebnis je Stop-Trade ab Einstieg, netto (nachgezogener Stop) | +34.3 % | -18.1 % |

Größte Lücken (pessimistisch): ETHEUR 2020-08-02: -16.3 %; ETHEUR 2025-10-10: -9.5 %; ETHEUR 2019-07-14: -7.4 %.

Schlechtester Einzeltrade netto (alle Ausstiege): -18.1 %.

| Ebene | historisch schlimmster Wert | Grenze laut Regel |
|---|---|---|
| Tag (Strategie, Schluss zu Schluss) | -17.1 % | keine Tagesgrenze im Trend-Bot |
| Tag (Kaufen & Halten 50/50) | -41.0 % | – |
| Position ab Einstieg (Stop 20 % + 0,4 % + 0,8 % Gebühren + Lücke) | -18.1 % | ≈ -37.5 % |
| Portfolio, Max. Drawdown | -37.6 % | – |
| Korrelation BTC/ETH (Tagesrenditen) | 0.67 | – |

- 19 €: beide Stops gleichzeitig, schlechteste gemessene Lücke → ≈ 7.13 € Verlust ab Einstieg.
- 100 €: beide Stops gleichzeitig, schlechteste gemessene Lücke → ≈ 37.52 € Verlust ab Einstieg.
- 500 €: beide Stops gleichzeitig, schlechteste gemessene Lücke → ≈ 187.62 € Verlust ab Einstieg.
- 1000 €: beide Stops gleichzeitig, schlechteste gemessene Lücke → ≈ 375.24 € Verlust ab Einstieg.

## 2. Robustheit der 200-Tage-Trendfolge

### Kalenderjahre (getrennt, nicht überlappend)

| Jahr | Trend 100 % | Trend 50 % | Kaufen & Halten 50/50 | Kaufen & Halten mit 32% investiert | investiert (Trend 100 %) |
|---|---|---|---|---|---|
| 2016 | +31.9 % | +15.6 % | +70.4 % | +23.5 % | 33% |
| 2017 | +91.3 % | +48.4 % | +3490.4 % | +251.6 % | 7% |
| 2018 | -6.8 % | -3.5 % | -79.8 % | -32.7 % | 6% |
| 2019 | +46.2 % | +26.1 % | +32.2 % | +15.6 % | 30% |
| 2020 | +119.0 % | +64.1 % | +348.0 % | +75.4 % | 57% |
| 2021 | +17.9 % | +10.5 % | +288.3 % | +69.4 % | 24% |
| 2022 | -9.7 % | -4.9 % | -64.9 % | -23.1 % | 2% |
| 2023 | +41.1 % | +20.9 % | +98.2 % | +26.6 % | 71% |
| 2024 | +72.2 % | +38.0 % | +74.8 % | +23.6 % | 49% |
| 2025 | +0.3 % | +0.3 % | -20.1 % | -3.2 % | 46% |
| 2026 (bis 17.10.) | +12.9 % | +6.5 % | -11.1 % | -1.3 % | 20% |
| Jahre im Plus | 9/11 | 9/11 | 7/11 | 7/11 | |

### Unabhängige Zeitblöcke und risikobereinigte Kennzahlen

**2016-03–2019 (Auswahl)**

| | Rendite p.a. | Max. Drawdown | Sharpe (rf 0) | Sortino | Calmar |
|---|---|---|---|---|---|
| Trend 100 % | +38.0 % | -34.8 % | 1.14 | 0.91 | 1.09 |
| Trend 50 % | +21.2 % | -22.2 % | 1.06 | 0.80 | 0.95 |
| Kaufen & Halten 50/50 | +107.3 % | -90.9 % | 1.26 | 1.79 | 1.18 |
| Kaufen & Halten mit 32% investiert | +37.4 % | -48.3 % | 1.26 | 1.79 | 0.77 |

**2020–2022 (Auswahl)**

| | Rendite p.a. | Max. Drawdown | Sharpe (rf 0) | Sortino | Calmar |
|---|---|---|---|---|---|
| Trend 100 % | +32.7 % | -29.7 % | 1.03 | 0.79 | 1.10 |
| Trend 50 % | +19.9 % | -17.2 % | 1.03 | 0.77 | 1.16 |
| Kaufen & Halten 50/50 | +82.4 % | -75.6 % | 1.14 | 1.48 | 1.09 |
| Kaufen & Halten mit 32% investiert | +31.6 % | -33.1 % | 1.14 | 1.48 | 0.95 |

**2023–10/2026 (Test)**

| | Rendite p.a. | Max. Drawdown | Sharpe (rf 0) | Sortino | Calmar |
|---|---|---|---|---|---|
| Trend 100 % | +30.6 % | -24.7 % | 1.00 | 1.15 | 1.24 |
| Trend 50 % | +16.5 % | -16.1 % | 0.93 | 1.03 | 1.02 |
| Kaufen & Halten 50/50 | +26.6 % | -61.3 % | 0.70 | 1.05 | 0.43 |
| Kaufen & Halten mit 32% investiert | +11.1 % | -23.8 % | 0.70 | 1.05 | 0.47 |

**gesamt**

| | Rendite p.a. | Max. Drawdown | Sharpe (rf 0) | Sortino | Calmar |
|---|---|---|---|---|---|
| Trend 100 % | +33.8 % | -37.6 % | 1.06 | 0.97 | 0.90 |
| Trend 50 % | +19.1 % | -23.7 % | 1.01 | 0.89 | 0.81 |
| Kaufen & Halten 50/50 | +67.8 % | -90.9 % | 1.06 | 1.44 | 0.75 |
| Kaufen & Halten mit 32% investiert | +25.9 % | -48.3 % | 1.06 | 1.44 | 0.54 |

Investitionsgrad Trend 100 %: Ø 32% der Tage (Rest Bargeld ohne Zins). Trades gesamt: 72 (≈ 6.8 pro Jahr).

## 3. Gebühren und Kapital: Was ist bei 19 / 100 / 500 / 1.000 € ausführbar?

Kraken heute (öffentliche API): Mindestorder, Spread und Preiseinfluss einer 1.000-€-Market-Order.

| Paar | Mindestorder | Spread | Preiseinfluss 1.000 € |
|---|---|---|---|
| XBTEUR | 4.39 € | 0.01 Bp. | 0.01 Bp. |
| ETHEUR | 4.31 € | 0.46 Bp. | 0.39 Bp. |

Gebühren Kraken Stufe 1: 0.40% Maker / 0.80% Taker → Kauf+Verkauf 0.8%–1.6%. Spread und Preiseinfluss sind bei diesen Größen gegenüber den Gebühren vernachlässigbar (< 0,05 %). Trendfolge: Gebühren ≈ 3.0% des Ø-Kapitals pro Jahr.

| Strategie | Ordergröße | 19 € | 100 € | 500 € | 1.000 € | Kosten je Runde |
|---|---|---|---|---|---|---|
| Trendfolge 100 % | 50.00 € bei 100 € | ✅ | ✅ | ✅ | ✅ | 0,8–1,6 % |
| Trendfolge 50 % | 25.00 € bei 100 € | ✅ | ✅ | ✅ | ✅ | 0,8–1,6 % |
| Grid (5 Stufen je Coin) | 10.00 € bei 100 € | ❌ (1.90 € < 4.42 €) | ✅ | ✅ | ✅ | 0,8 % (Ziel 3 %) |
| Daytrading-Studie (volles Kapital) | 50.00 € bei 100 € | ✅ | ✅ | ✅ | ✅ | 0,8–1,6 % |
| Signal-Bot (1 % Risiko, Stop ≈ 15 %) | 6.67 € bei 100 € | ❌ (1.27 € < 4.42 €) | ✅ | ✅ | ✅ | 0,8–1,6 % |

## 6. Copy-Trading-Simulation: Audit

Daten: 153,833 Trades aus 26 Pools, 17.10. 02:13 – 18.10. 10:37 UTC.

**Beobachtungsverzögerung** (Trade auf der Blockchain → erstmals von unserem Sammler gesehen, Untergrenze, 148,531 Trades): Median ≥ 100 s, 90 % ≥ 244 s. Jeder Pool wird im Median nur alle 276 s abgefragt; 8% der Abrufe hatten keine Überlappung mit dem vorigen (dazwischen verpasste Trades). Der Simulator nimmt 10 s an.

| Variante | Endwert (Start 100 $) | Trades | Gewinner | veraltete Ausstiegspreise | ohne spätere Verkäufe im Pool |
|---|---|---|---|---|---|
| Beste Wallets, 10 s | 103.02 $ | 19 | 8 | 5 | 0 |
| Beste Wallets, 100 s (gemessen) | 99.85 $ | 19 | 8 | 5 | 0 |
| Beste Wallets, 244 s (gemessen) | 98.35 $ | 19 | 6 | 5 | 0 |
| Zufällige Wallets (Seed 0), 10 s | 84.44 $ | 31 | 2 | 5 | 0 |
| Zufällige Wallets (Seed 1), 10 s | 93.82 $ | 35 | 15 | 5 | 0 |
| Zufällige Wallets (Seed 2), 10 s | 87.40 $ | 30 | 5 | 5 | 0 |
| Zufällige Wallets (Seed 3), 10 s | 96.70 $ | 31 | 8 | 5 | 0 |
| Zufällige Wallets (Seed 4), 10 s | 88.39 $ | 32 | 4 | 4 | 0 |

