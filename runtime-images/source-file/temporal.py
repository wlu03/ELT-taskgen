"""Encode native Parquet temporal values in Airbyte JSON records."""
from datetime import date, time

import numpy as np
import pandas as pd


def normalize_record(value):
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else pd.Timestamp(value).isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: normalize_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_record(item) for item in value]
    return value
