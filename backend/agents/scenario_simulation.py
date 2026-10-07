"""
Scenario Simulation Agent (Monte Carlo)
------------------------------------------
Simulates thousands of possible futures for the recommended portfolio.

What changed vs. v1 (and why):
  * MONTHLY steps instead of yearly, so monthly SIP contributions can be added
    the way people actually invest.
  * Fat tails: monthly returns are drawn from a Student-t distribution
    (df = 5) instead of a normal distribution. Normal curves badly
    under-estimate how often crashes like 2008 or March 2020 happen.
  * Log-returns with a drift correction (μ − σ²/2), so the MEDIAN outcome is
    realistic and a −120% year is impossible.
  * Inflation: every result is shown in today's rupees too. ₹2 lakh in 15
    years buys far less than ₹2 lakh today.
  * "Probability of loss" now compares against the TOTAL amount invested
    (lump sum + all SIPs), and a second figure shows the chance of losing
    to inflation.
  * Inputs come from the equilibrium expected return and covariance-aware
    volatility, so the projections no longer explode after a bull market.
"""
import numpy as np

from config import MONTE_CARLO_SIMULATIONS, STUDENT_T_DF, INFLATION_RATE


def run(initial_amount: float, monthly_sip: float, expected_annual_return: float,
        annual_volatility: float, horizon_years: int, seed: int = 42) -> dict:
    n_sims = MONTE_CARLO_SIMULATIONS
    months = max(1, int(horizon_years) * 12)
    rng = np.random.default_rng(seed)

    sigma_m = annual_volatility / np.sqrt(12)
    mu_log_m = np.log1p(expected_annual_return) / 12 - 0.5 * sigma_m ** 2
    df = STUDENT_T_DF
    shocks = rng.standard_t(df, size=(n_sims, months)) / np.sqrt(df / (df - 2))  # unit variance
    growth = np.exp(mu_log_m + sigma_m * shocks)

    values = np.empty((n_sims, months + 1))
    values[:, 0] = initial_amount
    for m in range(months):
        values[:, m + 1] = (values[:, m] + monthly_sip) * growth[:, m]  # SIP invested at month start

    invested = initial_amount + monthly_sip * np.arange(months + 1)
    deflator = (1 + INFLATION_RATE) ** (np.arange(months + 1) / 12)
    final = values[:, -1]
    final_real = final / deflator[-1]

    pct = lambda arr, q: float(np.percentile(arr, q))
    year_idx = np.arange(0, months + 1, 12)
    if year_idx[-1] != months:
        year_idx = np.append(year_idx, months)

    bands = {q: np.percentile(values[:, year_idx], q, axis=0) for q in (5, 25, 50, 75, 95)}
    total_invested = float(invested[-1])

    return {
        "initial_amount": round(initial_amount, 2),
        "monthly_sip": round(monthly_sip, 2),
        "horizon_years": horizon_years,
        "num_simulations": n_sims,
        "distribution": f"Student-t (df={df}), monthly steps",
        "expected_annual_return": round(expected_annual_return, 4),
        "annual_volatility": round(annual_volatility, 4),
        "inflation_rate": INFLATION_RATE,
        "total_invested": round(total_invested, 2),
        "final_value_percentiles": {
            "p5": round(pct(final, 5), 2), "p25": round(pct(final, 25), 2),
            "p50_median": round(pct(final, 50), 2),
            "p75": round(pct(final, 75), 2), "p95": round(pct(final, 95), 2),
        },
        "final_value_percentiles_real": {
            "p5": round(pct(final_real, 5), 2), "p50_median": round(pct(final_real, 50), 2),
            "p95": round(pct(final_real, 95), 2),
        },
        "yearly": {
            "years": [round(m / 12, 2) for m in year_idx],
            "p5": [round(float(v), 2) for v in bands[5]],
            "p25": [round(float(v), 2) for v in bands[25]],
            "p50": [round(float(v), 2) for v in bands[50]],
            "p75": [round(float(v), 2) for v in bands[75]],
            "p95": [round(float(v), 2) for v in bands[95]],
            "p50_real": [round(float(v), 2) for v in bands[50] / deflator[year_idx]],
            "invested": [round(float(v), 2) for v in invested[year_idx]],
        },
        "yearly_median_trajectory": [round(float(v), 2) for v in bands[50]],
        "probability_of_loss": round(float(np.mean(final < total_invested)), 4),
        "probability_of_losing_to_inflation": round(float(np.mean(final_real < total_invested)), 4),
    }
