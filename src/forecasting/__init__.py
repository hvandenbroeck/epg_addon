"""
Forecasting Module

This module contains all components related to predicting power usage for the next day.
It includes weather data fetching, historical statistics loading, and ML-based prediction.
"""

from .prediction import Prediction
from .statistics_loader import StatisticsLoader
from .weather import Weather
from .HAConfig import HAEnergyDashboardFetcher

__all__ = ['Prediction', 'StatisticsLoader', 'Weather', 'HAEnergyDashboardFetcher']
