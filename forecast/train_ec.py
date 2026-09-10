"""
Train EC's forecast model.

EC has only ~4 usable months (see forecast/build_estate_features.py), well under
monthly_model.MIN_TRAIN (6) — min_train=2 here lets the same walk-forward/conformal
pipeline K3 uses run on EC's thin, synthetic history instead of crashing on an empty
scoreboard. Expect very few backtest folds and correspondingly noisy accuracy/coverage
numbers; the model-health page reports the fold count honestly.

Run after forecast/fetch_nasa_weather.py and forecast/build_estate_features.py have
produced forecast/EC/weather_nasa_power_history.csv and
forecast/EC/features_estate_monthly.csv.
"""

import os

import monthly_model as mm

_EC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EC")

if __name__ == "__main__":
    mm.train(estate_dir=_EC_DIR, min_train=2, workdone_file=None, estate_label="ec")
