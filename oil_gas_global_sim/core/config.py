# core/config.py
from dataclasses import dataclass
from enum import Enum
from typing import Dict


class CommodityType(str, Enum):
    BRENT = "BRENT"              # Североморская эталонная легкая нефть
    URAL = "URALS"               # Российская экспортная высокосернистая смесь
    ESPO = "ESPO"                # Восточно-сибирская легкая нефть (ВСТО)
    ARAB_LIGHT = "ARAB_LIGHT"    # Саудовская легкая нефть
    WTI = "WTI"                  # Западно-техасская легкая нефть
    KASHAGAN_CPC = "CPC_BLEND"   # Каспийская смесь (КТК)
    LNG = "LNG"                  # Сжиженный природный газ (СПГ)
    PIPELINE_GAS = "PIPE_GAS"    # Трубопроводный природный газ


class VesselClass(str, Enum):
    VLCC = "VLCC"                # Very Large Crude Carrier (2 млн баррелей)
    SUEZMAX = "SUEZMAX"          # Максимальный размер для Суэцкого канала (1 млн баррелей)
    AFRAMAX = "AFRAMAX"          # Рабочая лошадка региональных морей (700 тыс. баррелей)
    LNG_QMAX = "LNG_QMAX"        # Криогенный газовоз СПГ
    ARC7_LNG = "ARC7_LNG"        # Арктический газовоз ледового класса Arc7 (Ямал)


@dataclass(frozen=True)
class CommoditySpec:
    name: str
    base_price_usd: float        # Базовая спотовая цена ($/баррель или $/MMBtu эквивалент)
    api_gravity: float           # Плотность по API (выше = легче)
    sulfur_pct: float            # Содержание серы (%)
    boil_off_rate_daily: float   # Потери на испарение в сутки (для LNG ~0.12% в день)
    is_cryogenic: bool = False


@dataclass(frozen=True)
class VesselSpec:
    vessel_class: VesselClass
    capacity_bbl: float          # Вместимость в баррелях нефтяного эквивалента
    speed_laden_knots: float     # Скорость с грузом (узлы, 1 узел = 1 морская миля/час)
    speed_ballast_knots: float   # Скорость порожняком (узлы)
    draft_laden_m: float         # Осадка в полном грузу (метры) — критично для проливов!
    draft_ballast_m: float       # Осадка в балласте (метры)
    bunker_burn_tons_day: float  # Расход мазута (тонн/сутки) на полном ходу
    daily_charter_usd: float     # Базовая ставка аренды (фрахта) в сутки ($)
    demurrage_rate_usd_hr: float # Штраф за простой в очереди у порта ($/час)
    ice_class: bool = False      # Способность идти по СМП без ледокола
    can_carry_lng: bool = False


COMMODITY_SPECS: Dict[CommodityType, CommoditySpec] = {
    CommodityType.BRENT: CommoditySpec("Brent Crude", 82.5, 38.0, 0.37, 0.0),
    CommodityType.URAL: CommoditySpec("Urals Blend", 68.0, 31.7, 1.45, 0.0),
    CommodityType.ESPO: CommoditySpec("ESPO Blend", 76.5, 34.8, 0.55, 0.0),
    CommodityType.ARAB_LIGHT: CommoditySpec("Arabian Light", 83.0, 33.0, 1.80, 0.0),
    CommodityType.WTI: CommoditySpec("West Texas Intermediate", 78.5, 39.6, 0.24, 0.0),
    CommodityType.KASHAGAN_CPC: CommoditySpec("CPC Blend", 81.0, 45.3, 0.56, 0.0),
    CommodityType.LNG: CommoditySpec("Liquefied Natural Gas", 95.0, 60.0, 0.0, 0.0012, is_cryogenic=True),
    CommodityType.PIPELINE_GAS: CommoditySpec("Natural Gas (Pipe)", 55.0, 65.0, 0.0, 0.0),
}

VESSEL_SPECS: Dict[VesselClass, VesselSpec] = {
    VesselClass.VLCC: VesselSpec(
        vessel_class=VesselClass.VLCC,
        capacity_bbl=2_000_000,
        speed_laden_knots=13.5,
        speed_ballast_knots=15.0,
        draft_laden_m=21.5,      # Не проходит Суэц (20.1м) и Датские проливы (15.0м) в полном грузу!
        draft_ballast_m=10.5,
        bunker_burn_tons_day=65.0,
        daily_charter_usd=48_000,
        demurrage_rate_usd_hr=2_500,
    ),
    VesselClass.SUEZMAX: VesselSpec(
        vessel_class=VesselClass.SUEZMAX,
        capacity_bbl=1_000_000,
        speed_laden_knots=14.0,
        speed_ballast_knots=15.5,
        draft_laden_m=17.0,      # Свободно проходит Суэцкий канал
        draft_ballast_m=9.0,
        bunker_burn_tons_day=42.0,
        daily_charter_usd=36_000,
        demurrage_rate_usd_hr=1_800,
    ),
    VesselClass.AFRAMAX: VesselSpec(
        vessel_class=VesselClass.AFRAMAX,
        capacity_bbl=700_000,
        speed_laden_knots=14.5,
        speed_ballast_knots=16.0,
        draft_laden_m=13.5,      # Проходит Балтику (Датские проливы) и Босфор
        draft_ballast_m=7.5,
        bunker_burn_tons_day=32.0,
        daily_charter_usd=28_000,
        demurrage_rate_usd_hr=1_400,
    ),
    VesselClass.LNG_QMAX: VesselSpec(
        vessel_class=VesselClass.LNG_QMAX,
        capacity_bbl=1_550_000,  # ~266,000 м3 СПГ в пересчете на б.н.э.
        speed_laden_knots=19.0,
        speed_ballast_knots=20.0,
        draft_laden_m=12.2,
        draft_ballast_m=9.4,
        bunker_burn_tons_day=55.0,
        daily_charter_usd=75_000,
        demurrage_rate_usd_hr=4_000,
        can_carry_lng=True,
    ),
    VesselClass.ARC7_LNG: VesselSpec(
        vessel_class=VesselClass.ARC7_LNG,
        capacity_bbl=1_050_000,  # ~172,000 м3 СПГ (ледокольный танкер Кристоф де Маржери)
        speed_laden_knots=17.5,
        speed_ballast_knots=18.5,
        draft_laden_m=12.0,
        draft_ballast_m=9.5,
        bunker_burn_tons_day=60.0,
        daily_charter_usd=88_000,
        demurrage_rate_usd_hr=4_500,
        ice_class=True,
        can_carry_lng=True,
    ),
}

# Глобальные экономические константы
BUNKER_FUEL_PRICE_USD_TON = 620.0  # Цена судового топлива VLSFO ($/тонна)
EARTH_RADIUS_NM = 3440.065         # Радиус Земли в морских милях
