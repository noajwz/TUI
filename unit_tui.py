#!/usr/bin/env python3
"""Unit converter — type what you'd say: `5 ft 11 in to cm`, `100 km/h in mph`, `72 F`, `1 cup flour
to g`, `8 L/100km to mpg`, `1.5 GiB to MB`, `3 h 20 min / 4`, `0xff to bin`, `100 usd to eur`.

Units are checked by dimension (length, mass, time, current, temperature, amount, light, angle,
information, money), so compound units work without being listed — kg·m/s², J/(kg K), lbf ft — and
arithmetic on quantities too. The table below the input shows the value in every unit of its kind
and updates as you type. Temperatures convert with their offsets; fuel economy converts between
L/100 km and mpg (one is the reciprocal of the other); cups of flour become grams with an
ingredient's density; currencies use the ECB's daily reference rates, fetched only when asked for.
"""

import argparse
import curses
import locale
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

DIGITS = 6                   # significant digits shown, ^P cycles through PRECISIONS
PRECISIONS = (4, 6, 8, 10, 15)
RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT = 10
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 60, 16
CAT_W = 20                   # width of the category list

# dimensions, as exponents of: metre, kilogram, second, ampere, kelvin, mole, candela, radian, bit, euro
DIMS = ("m", "kg", "s", "A", "K", "mol", "cd", "rad", "bit", "EUR")
NONE = (0,) * len(DIMS)

SI_PREFIXES = {"Q": 1e30, "R": 1e27, "Y": 1e24, "Z": 1e21, "E": 1e18, "P": 1e15, "T": 1e12, "G": 1e9, "M": 1e6,
               "k": 1e3, "h": 1e2, "da": 1e1, "d": 1e-1, "c": 1e-2, "m": 1e-3, "µ": 1e-6, "μ": 1e-6, "u": 1e-6,
               "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "z": 1e-21, "y": 1e-24, "r": 1e-27, "q": 1e-30}
BIN_PREFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60, "Zi": 2**70, "Yi": 2**80}
PREFIX_NAMES = {"quetta": 1e30, "ronna": 1e27, "yotta": 1e24, "zetta": 1e21, "exa": 1e18, "peta": 1e15, "tera": 1e12,
                "giga": 1e9, "mega": 1e6, "kilo": 1e3, "hecto": 1e2, "deca": 1e1, "deka": 1e1, "deci": 1e-1,
                "centi": 1e-2, "milli": 1e-3, "micro": 1e-6, "nano": 1e-9, "pico": 1e-12, "femto": 1e-15,
                "atto": 1e-18, "kibi": 2**10, "mebi": 2**20, "gibi": 2**30, "tebi": 2**40, "pebi": 2**50}

# (symbol, names, definition, flags) per category. A definition starting with ! is a base dimension;
# the rest are expressions in units defined above them. Flags: p takes SI prefixes, b binary ones too.
UNITS = {
    "Length": [
        ("m", "metre meter metres meters", "!m", "p"),
        ("in", "inch inches", "0.0254 m", ""),
        ("ft", "foot feet", "12 in", ""),
        ("yd", "yard yards", "3 ft", ""),
        ("mi", "mile miles", "5280 ft", ""),
        ("nmi", "nautical_mile", "1852 m", ""),
        ("Å", "angstrom ångström Å", "1e-10 m", ""),
        ("micron", "microns", "1e-6 m", ""),
        ("mil", "thou mils", "0.001 in", ""),
        ("hand", "hands", "4 in", ""),
        ("fathom", "fathoms", "6 ft", ""),
        ("chain", "chains", "66 ft", ""),
        ("rod", "rods perch", "16.5 ft", ""),
        ("furlong", "furlongs", "660 ft", ""),
        ("league", "leagues", "3 mi", ""),
        ("au", "astronomical_unit AU", "149597870700 m", ""),
        ("ly", "lightyear light_year", "9460730472580800 m", ""),
        ("pc", "parsec parsecs", "3.0856775814913673e16 m", "p"),
    ],
    "Area": [
        ("ha", "hectare hectares", "10000 m^2", ""),
        ("are", "ares", "100 m^2", ""),
        ("acre", "acres", "43560 ft^2", ""),
        ("sqft", "", "ft^2", ""),
        ("sqm", "", "m^2", ""),
        ("barn", "barns", "1e-28 m^2", ""),
    ],
    "Volume": [
        ("L", "l litre liter litres liters", "0.001 m^3", "p"),
        ("cc", "", "cm^3", ""),
        ("gal", "gallon gallons", "231 in^3", ""),
        ("qt", "quart quarts", "gal/4", ""),
        ("pint", "pt pints", "gal/8", ""),
        ("cup", "cups", "gal/16", ""),
        ("floz", "fluid_ounce", "gal/128", ""),
        ("tbsp", "tablespoon tablespoons tbs eetlepel", "floz/2", ""),
        ("tsp", "teaspoon teaspoons theelepel", "tbsp/3", ""),
        ("impgal", "imperial_gallon", "4.54609 L", ""),
        ("imppt", "imperial_pint", "impgal/8", ""),
        ("impfloz", "imperial_fluid_ounce", "impgal/160", ""),
        ("mcup", "metric_cup", "250 mL", ""),
        ("bbl", "barrel barrels", "42 gal", ""),
    ],
    "Mass": [
        ("g", "gram grams gramme", "!kg 0.001", "p"),
        ("t", "tonne tonnes metric_ton", "1000 kg", ""),
        ("lb", "pound pounds lbs", "0.45359237 kg", ""),
        ("oz", "ounce ounces", "lb/16", ""),
        ("st", "stone stones", "14 lb", ""),
        ("gr", "grain grains", "lb/7000", ""),
        ("ct", "carat carats", "0.2 g", ""),
        ("ozt", "troy_ounce", "31.1034768 g", ""),
        ("shortton", "short_ton", "2000 lb", ""),
        ("longton", "long_ton", "2240 lb", ""),
        ("ons", "", "100 g", ""),
        ("pond", "", "500 g", ""),
        ("Da", "dalton amu u", "1.66053906660e-27 kg", "p"),
        ("slug", "slugs", "14.59390294 kg", ""),
    ],
    "Time": [
        ("s", "second seconds sec secs", "!s", "p"),
        ("min", "minute minutes mins", "60 s", ""),
        ("h", "hour hours hr hrs", "60 min", ""),
        ("d", "day days", "24 h", ""),
        ("wk", "week weeks", "7 d", ""),
        ("fortnight", "fortnights", "14 d", ""),
        ("mo", "month months", "30.436875 d", ""),
        ("yr", "year years", "365.2425 d", ""),
        ("decade", "decades", "10 yr", ""),
        ("century", "centuries", "100 yr", ""),
        ("millennium", "millennia", "1000 yr", ""),
    ],
    "Temperature": [
        ("K", "kelvin kelvins", "!K", "p"),
        ("degC", "°C ℃ celsius centigrade", "K", ""),
        ("degF", "°F ℉ fahrenheit", "5/9 K", ""),
        ("degR", "°R rankine", "5/9 K", ""),
    ],
    "Speed": [
        ("mph", "", "mi/h", ""),
        ("kph", "kmh", "km/h", ""),
        ("kn", "knot knots kt", "nmi/h", ""),
        ("c", "lightspeed speed_of_light", "299792458 m/s", ""),
        ("mach", "", "343 m/s", ""),
    ],
    "Fuel economy": [
        ("mpg", "", "mi/gal", ""),
        ("mpguk", "mpg_uk", "mi/impgal", ""),
    ],
    "Acceleration": [
        ("g0", "gee standard_gravity", "9.80665 m/s^2", ""),
        ("Gal", "gal_accel", "0.01 m/s^2", ""),
    ],
    "Force": [
        ("N", "newton newtons", "kg m/s^2", "p"),
        ("lbf", "pound_force", "4.4482216152605 N", ""),
        ("kgf", "kilogram_force kp", "9.80665 N", ""),
        ("dyn", "dyne dynes", "1e-5 N", ""),
        ("pdl", "poundal poundals", "0.138254954376 N", ""),
    ],
    "Pressure": [
        ("Pa", "pascal pascals", "N/m^2", "p"),
        ("bar", "bars", "1e5 Pa", "p"),
        ("atm", "atmosphere atmospheres", "101325 Pa", ""),
        ("psi", "", "lbf/in^2", "p"),
        ("torr", "Torr", "101325/760 Pa", ""),
        ("mmHg", "", "133.322387415 Pa", ""),
        ("inHg", "", "3386.389 Pa", ""),
        ("cmH2O", "", "98.0665 Pa", ""),
    ],
    "Energy": [
        ("J", "joule joules", "N m", "p"),
        ("cal", "calorie calories", "4.184 J", "p"),
        ("Cal", "food_calorie kcal_food", "1000 cal", ""),
        ("Wh", "watt_hour", "3600 J", "p"),
        ("eV", "electronvolt electron_volt", "1.602176634e-19 J", "p"),
        ("BTU", "btu Btu", "1055.05585262 J", ""),
        ("therm", "therms", "105505585.262 J", ""),
        ("erg", "ergs", "1e-7 J", ""),
        ("ftlbf", "foot_pound", "1.3558179483314 J", ""),
        ("tTNT", "ton_of_tnt", "4.184e9 J", ""),
        ("toe", "tonne_of_oil_equivalent", "41.868e9 J", ""),
    ],
    "Power": [
        ("W", "watt watts", "J/s", "p"),
        ("hp", "horsepower", "745.69987158227022 W", ""),
        ("PS", "pk metric_horsepower", "735.49875 W", ""),
        ("RT", "ton_of_refrigeration", "3516.853 W", ""),
    ],
    "Electric current": [("A", "amp amps ampere amperes", "!A", "p")],
    "Charge": [
        ("C", "coulomb coulombs", "A s", "p"),
        ("Ah", "amp_hour", "3600 C", "p"),
    ],
    "Voltage": [("V", "volt volts", "W/A", "p")],
    "Resistance": [("Ω", "ohm ohms Ohm", "V/A", "p")],
    "Conductance": [("S", "siemens", "A/V", "p")],
    "Capacitance": [("F", "farad farads", "C/V", "p")],
    "Inductance": [("H", "henry henries", "V s/A", "p")],
    "Magnetic flux": [("Wb", "weber webers", "V s", "p")],
    "Magnetic field": [
        ("T", "tesla teslas", "Wb/m^2", "p"),
        ("G", "gauss", "1e-4 T", "p"),
    ],
    "Amount": [("mol", "mole moles", "!mol", "p")],
    "Angle": [
        ("rad", "radian radians", "!rad", "p"),
        ("deg", "° degree degrees", "pi rad/180", ""),
        ("arcmin", "′ arcminute arcminutes", "deg/60", ""),
        ("arcsec", "″ arcsecond arcseconds", "arcmin/60", "p"),
        ("grad", "gon gradian gradians", "pi rad/200", ""),
        ("rev", "turn turns revolution revolutions cycle cycles", "2 pi rad", ""),
        ("sr", "steradian steradians", "rad^2", ""),
    ],
    "Angular speed": [
        ("rpm", "", "rev/min", ""),
        ("rps", "", "rev/s", ""),
    ],
    "Frequency": [("Hz", "hertz", "1/s", "p")],
    "Radioactivity": [
        ("Bq", "becquerel becquerels", "1/s", "p"),
        ("Ci", "curie curies", "3.7e10 Bq", "p"),
    ],
    "Radiation dose": [
        ("Gy", "gray grays", "J/kg", "p"),
        ("Sv", "sievert sieverts", "J/kg", "p"),
        ("rem", "", "0.01 Sv", "p"),
    ],
    "Light": [
        ("cd", "candela candelas", "!cd", "p"),
        ("lm", "lumen lumens", "cd sr", "p"),
        ("lx", "lux", "lm/m^2", "p"),
    ],
    "Data": [
        ("bit", "bits b", "!bit", "pb"),
        ("B", "byte bytes octet octets", "8 bit", "pb"),
        ("nibble", "nibbles", "4 bit", ""),
    ],
    "Data rate": [("bps", "", "bit/s", "p")],
    "Typography": [
        ("px", "pixel pixels", "in/96", ""),
        ("point", "points", "in/72", ""),
        ("pica", "picas", "12 point", ""),
    ],
    "Ratio": [
        ("%", "percent pct", "0.01", ""),
        ("‰", "permille", "0.001", ""),
        ("ppm", "", "1e-6", ""),
        ("ppb", "", "1e-9", ""),
        ("dozen", "dozens", "12", ""),
        ("gross", "", "144", ""),
    ],
    "Currency": [("EUR", "€ eur euro euros", "!EUR", "")],
}

# what each category's table lists: expression=name
TABLES = {
    "Length": "nm=nanometre|µm=micrometre|mm=millimetre|cm=centimetre|m=metre|km=kilometre|in=inch|ft=foot|yd=yard|"
              "mi=mile|nmi=nautical mile|mil=thou|Å=ångström|au=astronomical unit|ly=light-year|pc=parsec",
    "Area": "mm^2=square millimetre|cm^2=square centimetre|m^2=square metre|are=are|ha=hectare|"
              "km^2=square kilometre|in^2=square inch|ft^2=square foot|yd^2=square yard|acre=acre|mi^2=square mile",
    "Volume": "mL=millilitre|cL=centilitre|dL=decilitre|L=litre|m^3=cubic metre|cm^3=cubic centimetre|"
              "tsp=teaspoon (US)|tbsp=tablespoon (US)|floz=fluid ounce (US)|cup=cup (US)|mcup=metric cup|"
              "pint=pint (US)|qt=quart (US)|gal=gallon (US)|impfloz=fluid ounce (UK)|imppt=pint (UK)|"
              "impgal=gallon (UK)|in^3=cubic inch|ft^3=cubic foot|bbl=barrel (oil)",
    "Mass": "mg=milligram|g=gram|ons=ons (NL, 100 g)|pond=pond (NL, 500 g)|kg=kilogram|t=tonne|gr=grain|ct=carat|"
              "oz=ounce|ozt=troy ounce|lb=pound|st=stone|shortton=short ton (US)|longton=long ton (UK)|Da=dalton",
    "Time": "ns=nanosecond|µs=microsecond|ms=millisecond|s=second|min=minute|h=hour|d=day|wk=week|"
              "fortnight=fortnight|mo=month (average)|yr=year (Gregorian)|decade=decade|century=century",
    "Temperature": "degC=Celsius|degF=Fahrenheit|K=kelvin|degR=Rankine",
    "Speed": "m/s=metre per second|km/h=kilometre per hour|mph=mile per hour|kn=knot|ft/s=foot per second|"
              "mach=Mach (at 20 °C)|c=speed of light",
    "Acceleration": "m/s^2=metre per second²|ft/s^2=foot per second²|g0=standard gravity|Gal=gal",
    "Force": "N=newton|kN=kilonewton|dyn=dyne|lbf=pound-force|kgf=kilogram-force|pdl=poundal",
    "Pressure": "Pa=pascal|hPa=hectopascal|kPa=kilopascal|MPa=megapascal|mbar=millibar|bar=bar|atm=atmosphere|"
              "psi=pound per square inch|torr=torr|mmHg=millimetre of mercury|inHg=inch of mercury|cmH2O=cm of water",
    "Energy": "J=joule|kJ=kilojoule|MJ=megajoule|cal=calorie|kcal=kilocalorie (food Calorie)|Wh=watt-hour|"
              "kWh=kilowatt-hour|eV=electronvolt|BTU=British thermal unit|therm=therm|ftlbf=foot-pound|erg=erg|"
              "tTNT=ton of TNT|toe=tonne of oil equivalent",
    "Torque": "N m=newton-metre|kN m=kilonewton-metre|lbf ft=pound-foot|lbf in=pound-inch|kgf m=kilogram-metre",
    "Power": "mW=milliwatt|W=watt|kW=kilowatt|MW=megawatt|hp=horsepower|PS=metric horsepower (pk)|"
              "BTU/h=BTU per hour|RT=ton of refrigeration",
    "Fuel economy": "L/(100 km)=litres per 100 km|km/L=kilometres per litre|mpg=mile per gallon (US)|"
              "mpguk=mile per gallon (UK)",
    "Density": "kg/m^3=kilogram per cubic metre|g/cm^3=gram per cm³|g/mL=gram per millilitre|"
              "kg/L=kilogram per litre|lb/ft^3=pound per cubic foot|lb/in^3=pound per cubic inch|"
              "lb/gal=pound per gallon (US)",
    "Flow": "m^3/s=cubic metre per second|m^3/h=cubic metre per hour|L/s=litre per second|L/min=litre per minute|"
              "gal/min=gallon per minute (US)|ft^3/min=cubic foot per minute",
    "Data": "bit=bit|B=byte|kB=kilobyte|MB=megabyte|GB=gigabyte|TB=terabyte|PB=petabyte|KiB=kibibyte|MiB=mebibyte|"
              "GiB=gibibyte|TiB=tebibyte|PiB=pebibyte|kbit=kilobit|Mbit=megabit|Gbit=gigabit",
    "Data rate": "bit/s=bit per second|kbit/s=kilobit per second|Mbit/s=megabit per second|"
              "Gbit/s=gigabit per second|B/s=byte per second|kB/s=kilobyte per second|MB/s=megabyte per second|"
              "GB/s=gigabyte per second|MiB/s=mebibyte per second",
    "Frequency": "Hz=hertz|kHz=kilohertz|MHz=megahertz|GHz=gigahertz|THz=terahertz|1/min=per minute|"
              "rpm=revolution per minute",
    "Angle": "rad=radian|mrad=milliradian|deg=degree|arcmin=arcminute|arcsec=arcsecond|grad=gradian|rev=turn",
    "Angular speed": "rad/s=radian per second|deg/s=degree per second|rpm=revolution per minute|"
              "rps=revolution per second|Hz=hertz",
    "Electric current": "µA=microampere|mA=milliampere|A=ampere|kA=kiloampere",
    "Charge": "C=coulomb|mC=millicoulomb|mAh=milliampere-hour|Ah=ampere-hour|e=elementary charge",
    "Voltage": "µV=microvolt|mV=millivolt|V=volt|kV=kilovolt|MV=megavolt",
    "Resistance": "mΩ=milliohm|Ω=ohm|kΩ=kiloohm|MΩ=megaohm",
    "Conductance": "µS=microsiemens|mS=millisiemens|S=siemens",
    "Capacitance": "pF=picofarad|nF=nanofarad|µF=microfarad|mF=millifarad|F=farad",
    "Inductance": "nH=nanohenry|µH=microhenry|mH=millihenry|H=henry",
    "Magnetic flux": "Wb=weber|mWb=milliweber",
    "Magnetic field": "µT=microtesla|mT=millitesla|T=tesla|G=gauss",
    "Amount": "mmol=millimole|mol=mole|kmol=kilomole",
    "Radioactivity": "Bq=becquerel|kBq=kilobecquerel|MBq=megabecquerel|GBq=gigabecquerel|Ci=curie|mCi=millicurie|"
              "µCi=microcurie",
    "Radiation dose": "Gy=gray|mGy=milligray|Sv=sievert|mSv=millisievert|µSv=microsievert|rem=rem|mrem=millirem",
    "Light": "cd=candela|lm=lumen|lx=lux",
    "Typography": "px=pixel (CSS, 96 per inch)|point=point (72 per inch)|pica=pica|mm=millimetre|cm=centimetre|"
              "in=inch",
    "Ratio": "1=as a number|%=percent|‰=per mille|ppm=parts per million|ppb=parts per billion|dozen=dozen|"
              "gross=gross",
}
CATEGORY_ORDER = ["Length", "Area", "Volume", "Mass", "Time", "Temperature", "Speed", "Acceleration", "Force",
                  "Pressure", "Energy", "Torque", "Power", "Fuel economy", "Density", "Flow", "Data", "Data rate",
                  "Frequency", "Angle", "Angular speed", "Electric current", "Charge", "Voltage", "Resistance",
                  "Conductance", "Capacitance", "Inductance", "Magnetic flux", "Magnetic field", "Amount",
                  "Radioactivity", "Radiation dose", "Light", "Typography", "Ratio", "Currency", "Number"]

# grams per millilitre, for turning cups and spoons into grams
INGREDIENTS = {"water": 1.0, "milk": 1.03, "buttermilk": 1.03, "cream": 1.01, "yogurt": 1.03, "yoghurt": 1.03,
               "all-purpose flour": 0.53, "bread flour": 0.54, "whole wheat flour": 0.51, "almond flour": 0.40,
               "flour": 0.53, "brown sugar": 0.93, "powdered sugar": 0.51, "icing sugar": 0.51, "sugar": 0.845,
               "butter": 0.959, "oil": 0.92, "olive oil": 0.91, "honey": 1.42, "maple syrup": 1.32, "syrup": 1.37,
               "rice": 0.78, "oats": 0.38, "salt": 1.22, "cocoa": 0.42, "cornstarch": 0.54, "baking powder": 0.97,
               "baking soda": 0.92, "peanut butter": 1.08, "chocolate chips": 0.72, "coffee": 0.36, "tea": 0.38,
               "raisins": 0.65, "walnuts": 0.42, "almonds": 0.6, "cheese": 0.47, "parmesan": 0.42, "wine": 0.99,
               "beer": 1.01, "gasoline": 0.745, "petrol": 0.745, "diesel": 0.832, "ethanol": 0.789, "mercury": 13.534,
               "ice": 0.917, "sand": 1.6, "concrete": 2.4, "steel": 7.85, "aluminium": 2.7, "aluminum": 2.7,
               "gold": 19.32, "silver": 10.49, "copper": 8.96, "lead": 11.34}

# multi-word names and phrases, rewritten before parsing (case-insensitive)
PHRASES = [
    (r"imperial fluid ounces?|uk fluid ounces?", "impfloz"), (r"fluid ounces?|fl\.? ?oz\.?", "floz"),
    (r"imperial gallons?|uk gallons?", "impgal"), (r"imperial pints?|uk pints?", "imppt"),
    (r"nautical miles?", "nmi"), (r"light[- ]years?", "ly"), (r"astronomical units?", "au"),
    (r"metric tons?", "t"), (r"short tons?", "shortton"), (r"long tons?", "longton"), (r"troy ounces?", "ozt"),
    (r"miles? per hour", "mph"), (r"kilomet(?:er|re)s? per hour", "km/h"), (r"miles? per gallon", "mpg"),
    (r"degrees? (?:celsius|centigrade)", "degC"), (r"degrees? fahrenheit", "degF"), (r"degrees? rankine", "degR"),
    (r"metric horse ?power", "PS"), (r"horse ?power", "hp"), (r"pounds?[- ]force", "lbf"),
    (r"kilograms?[- ]force", "kgf"), (r"foot[- ]pounds?", "ftlbf"), (r"pounds? per square inch", "psi"),
    (r"kilowatt[- ]hours?", "kWh"), (r"megawatt[- ]hours?", "MWh"), (r"watt[- ]hours?", "Wh"),
    (r"electron ?volts?", "eV"), (r"standard gravity", "g0"), (r"tons? of tnt", "tTNT"),
    (r"milliamp(?:ere)?[- ]hours?", "mAh"), (r"amp(?:ere)?[- ]hours?", "Ah"), (r"per ?cent", "%"),
    (r"per mille", "‰"), (r"bits? per second", "bit/s"), (r"bytes? per second", "B/s"),
    (r"speed of light", "c"), (r"metric cups?", "mcup"), (r"food calories?", "Cal"),
    (r"square (\w+)", r"\1^2"), (r"sq\.? (\w+)", r"\1^2"), (r"cubic (\w+)", r"\1^3"),
    (r"(\w+) squared", r"\1^2"), (r"(\w+) cubed", r"\1^3"), (r"\bper\b", "/"),
]
BASES = {"hex": 16, "hexadecimal": 16, "bin": 2, "binary": 2, "oct": 8, "octal": 8, "dec": 10, "decimal": 10,
         "roman": 0}
CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₺": "TRY", "zł": "PLN"}
ISO_CODES = set("""USD JPY BGN CZK DKK GBP HUF PLN RON SEK CHF ISK NOK TRY AUD BRL CAD CNY HKD IDR ILS INR KRW MXN
MYR NZD PHP SGD THB ZAR""".split())
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
SUPER_OUT = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
FRACTIONS = {"½": 1 / 2, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 1 / 4, "¾": 3 / 4, "⅕": 1 / 5, "⅛": 1 / 8, "⅜": 3 / 8,
             "⅝": 5 / 8, "⅞": 7 / 8, "⅙": 1 / 6}


class ConvError(Exception):
    pass


class Unit:
    def __init__(self, symbol, name, factor, dims, offset=0.0, category=None, flags=""):
        self.symbol, self.name, self.factor, self.dims = symbol, name, factor, dims
        self.offset, self.category, self.flags = offset, category, flags


SYMBOLS = {}     # exact symbol or alias -> Unit
NAMES = {}       # lower-case name -> Unit


def add_dims(a, b, k=1):
    return tuple(x + k * y for x, y in zip(a, b))


class Q:
    """A quantity: a value in SI units and its dimensions. `units` are the units it was written in,
    used to pick the category to show."""
    __slots__ = ("value", "dims", "units", "bare")

    def __init__(self, value, dims=NONE, units=(), bare=True):
        self.value, self.dims, self.units, self.bare = value, dims, tuple(units), bare

    def __mul__(self, o):
        return Q(self.value * o.value, add_dims(self.dims, o.dims), self.units + o.units, self.bare and o.bare)

    def __truediv__(self, o):
        if o.value == 0:
            raise ConvError("division by zero")
        return Q(self.value / o.value, add_dims(self.dims, o.dims, -1), self.units + o.units, self.bare and o.bare)

    def __pow__(self, n):
        if self.value < 0 and n != int(n):
            raise ConvError("fractional power of a negative number")
        return Q(self.value ** n, tuple(d * n for d in self.dims), self.units, self.bare)

    def __add__(self, o):
        if not same(self.dims, o.dims):
            raise ConvError(f"can't add {describe(o.dims)} to {describe(self.dims)}")
        return Q(self.value + o.value, self.dims, self.units + o.units, self.bare and o.bare)

    def __neg__(self):
        return Q(-self.value, self.dims, self.units, self.bare)


def same(a, b):
    return all(abs(x - y) < 1e-9 for x, y in zip(a, b))


# -- reading expressions --

TOKEN = re.compile(r"""
    (?P<hex>0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+)
  | (?P<num>\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?(?:[eE][-+]?\d+)?      # 1,000,000.5
          |\d[\d_]*(?:[.,]\d+)?(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)
  | (?P<id>(?:zł|[A-Za-zµμΩÅ°℃℉%‰$€£¥₹₩₺′″_])(?:[A-Za-zµμΩÅ°_]|\d(?=[A-Za-z]))*)
  | (?P<op>\*\*|[-+*/^()×·÷'"])
  | (?P<space>\s+)
""", re.X)


def tokenize(text):
    glyphs = "".join(FRACTIONS)
    text = re.sub(rf"(\d+)\s*([{glyphs}])", lambda m: f" {int(m[1]) + FRACTIONS[m[2]]!r} ", text)   # 3 ½
    text = re.sub(rf"[{glyphs}]", lambda m: f" {FRACTIONS[m[0]]!r} ", text)
    out, pos, degree_seen = [], 0, False
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise ConvError(f"don't understand “{text[pos:pos + 10].strip()}”")
        pos = m.end()
        kind = m.lastgroup
        tok = m[kind]
        if kind == "space":
            continue
        if kind == "hex":
            out.append(("num", float(int(tok.replace("_", ""), 0)), tok))
        elif kind == "num":
            if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[eE][-+]?\d+)?", tok):
                value = float(tok.replace(",", ""))
            else:
                value = float(tok.replace("_", "").replace(",", "."))   # a decimal comma: 1,5
            out.append(("num", value, tok))
        elif kind == "id":
            exp = re.match(r"\d+", text[pos:]) if tok[-1].isalpha() else None
            if exp and lookup(tok + exp[0]) is None:
                pos += exp.end()                         # m2, cm3: a power
                out += [("id", tok, tok), ("op", "^", "^"), ("num", float(exp[0]), exp[0])]
            elif exp:
                pos += exp.end()
                out.append(("id", tok + exp[0], tok + exp[0]))
            else:
                if tok in ("°", "deg"):
                    degree_seen = True
                out.append(("id", tok, tok))
            sup = re.match(r"[⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+", text[pos:])
            if sup:
                pos += sup.end()
                out += [("op", "^", "^"), ("num", float(sup[0].translate(SUPERSCRIPTS)), sup[0])]
        elif tok in ("'", '"'):
            # 5'11" is feet and inches; after a ° it's arcminutes and arcseconds
            name = ("arcmin" if tok == "'" else "arcsec") if degree_seen else ("ft" if tok == "'" else "in")
            out.append(("id", name, tok))
        else:
            out.append(("op", {"**": "^", "×": "*", "·": "*", "÷": "/"}.get(tok, tok), tok))
    # 1/2 and 3 1/2 directly before a unit are fractions, not a division by a quantity
    merged, i = [], 0
    while i < len(out):
        t = out[i]
        if (t[0] == "num" and i + 2 < len(out) and out[i + 1] == ("op", "/", "/") and out[i + 2][0] == "num"
                and (i + 3 >= len(out) or out[i + 3][0] == "id" or out[i + 3][1] in ("+", "-", ")"))
                and not (merged and merged[-1][1] in ("/", "^"))):
            if not out[i + 2][1]:
                raise ConvError("division by zero")
            value = t[1] / out[i + 2][1]
            if merged and merged[-1][0] == "num" and float(merged[-1][1]).is_integer():
                prev = merged.pop()                       # 3 1/2
                value = prev[1] + value
            merged.append(("num", value, f"{t[2]}/{out[i + 2][2]}"))
            i += 3
            continue
        merged.append(t)
        i += 1
    return merged


class Parser:
    def __init__(self, tokens):
        self.toks, self.i = tokens, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        if not self.toks:
            raise ConvError("nothing to convert")
        q = self.sum()
        if self.peek() is not None:
            raise ConvError(f"unexpected “{self.peek()[2]}”")
        return q

    def sum(self):
        q = self.product()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in "+-":
            op = self.take()[1]
            r = self.product()
            q = q + r if op == "+" else q + (-r)
        return q

    def product(self):
        q = self.juxt()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in "*/":
            op = self.take()[1]
            r = self.juxt()
            q = q * r if op == "*" else q / r
        return q

    def juxt(self):
        """Side by side multiplies — kg m — and binds tighter than / (J/kg K is J/(kg K)). A number
        after a unit starts a new term to add: 5 ft 11 in, 1 h 30 min."""
        terms, cur = [], None
        while True:
            t = self.peek()
            if t is None or (t[0] == "op" and t[1] not in "(-") or (t[0] == "op" and t[1] == "-" and cur is not None):
                break
            q = self.power()
            if cur is not None and q.bare and not cur.bare and t[0] == "num":
                terms.append(cur)
                cur = q
            else:
                cur = q if cur is None else cur * q
        if cur is None:
            t = self.peek()
            raise ConvError(f"expected a number or unit before “{t[2]}”" if t else "unfinished expression")
        for t in terms:
            cur = t + cur
        return cur

    def power(self):
        q = self.unary()
        t = self.peek()
        if t and t[0] == "op" and t[1] == "^":
            self.take()
            sign = 1
            if self.peek() and self.peek()[1] == "-":
                self.take()
                sign = -1
            if self.peek() and self.peek()[1] == "(":
                e = self.atom()
                if not e.bare:
                    raise ConvError("a power must be a plain number")
                n = e.value
            else:
                t = self.take()
                if not t or t[0] != "num":
                    raise ConvError("expected a number after ^")
                n = t[1]
            q = q ** (sign * n)
        return q

    def unary(self):
        t = self.peek()
        if t and t[0] == "op" and t[1] == "-":
            self.take()
            return -self.unary()
        if t and t[0] == "op" and t[1] == "+":
            self.take()
            return self.unary()
        return self.atom()

    def atom(self):
        t = self.take()
        if t is None:
            raise ConvError("unfinished expression")
        if t[0] == "num":
            return Q(t[1])
        if t[0] == "op" and t[1] == "(":
            q = self.sum()
            if not self.peek() or self.peek()[1] != ")":
                raise ConvError("missing )")
            self.take()
            return q
        if t[0] == "id":
            u = lookup(t[1])
            if u is None:
                raise ConvError(f"unknown unit “{t[1]}”")
            return Q(u.factor, u.dims, (u,), bare=(u.dims == NONE and u.symbol in ("pi",)))
        raise ConvError(f"unexpected “{t[2]}”")


def lookup(name):
    """A unit by symbol, name, plural, or with an SI or binary prefix."""
    if name in SYMBOLS:
        return SYMBOLS[name]
    low = name.lower()
    if low in NAMES:
        return NAMES[low]
    for cut in ("s", "es"):
        if low.endswith(cut) and low[:-len(cut)] in NAMES:
            return NAMES[low[:-len(cut)]]
    for prefixes, binary in ((BIN_PREFIXES, True), (SI_PREFIXES, False)):
        for p in sorted(prefixes, key=len, reverse=True):
            if name.startswith(p) and len(name) > len(p):
                u = SYMBOLS.get(name[len(p):])
                if u and "p" in u.flags and (not binary or "b" in u.flags) and not u.offset:
                    return Unit(name, p + u.name, u.factor * prefixes[p], u.dims, 0, u.category)
    for p, f in PREFIX_NAMES.items():
        if low.startswith(p) and len(low) > len(p):
            u = lookup(low[len(p):])
            if u and "p" in u.flags:
                return Unit(name, p + u.name, u.factor * f, u.dims, 0, u.category)
    if name == "KB":
        return lookup("kB")
    if name != low:
        return lookup(low)                   # KM, Kg
    return None


def define():
    SYMBOLS["pi"] = SYMBOLS["π"] = Unit("pi", "pi", math.pi, NONE, category="Ratio")
    for category, units in UNITS.items():
        for symbol, names, definition, flags in units:
            if definition.startswith("!"):
                base, _, scale = definition[1:].partition(" ")
                dims = tuple(1 if d == base else 0 for d in DIMS)
                factor, offset = float(scale or 1), 0.0
            else:
                q = Parser(tokenize(definition)).parse()
                factor, dims, offset = q.value, q.dims, 0.0
            if symbol == "degC":
                offset = 273.15
            elif symbol == "degF":
                offset = 459.67
            name = (names.split() or [symbol])[0].replace("_", " ")
            u = Unit(symbol, name, factor, dims, offset, category, flags)
            SYMBOLS[symbol] = u
            if len(symbol) >= 3:
                NAMES.setdefault(symbol.lower(), u)
            for alias in names.split():
                if not alias.isascii() or alias in ("b", "u", "pt", "kt", "kp", "pk", "Ohm", "Torr", "AU", "Btu"):
                    SYMBOLS.setdefault(alias, u)
                NAMES.setdefault(alias.lower().replace("_", " "), u)
                NAMES.setdefault(alias.lower(), u)


define()
SYMBOLS["e"] = Unit("e", "elementary charge", 1.602176634e-19, SYMBOLS["C"].dims, category="Charge")
SYMBOLS["mb"] = lookup("mbar")             # millibits are never what's meant


def describe(dims):
    """Dimensions in words when they're a known kind, else as base units."""
    cat = categories_for(dims)
    if cat:
        return cat[0].lower()
    if same(dims, NONE):
        return "a plain number"
    up = [f"{d}{'' if e == 1 else str(int(e) if float(e).is_integer() else e).translate(SUPER_OUT)}"
          for d, e in zip(DIMS, dims) if e > 0]
    down = [f"{d}{'' if e == -1 else str(int(-e) if float(e).is_integer() else -e).translate(SUPER_OUT)}"
            for d, e in zip(DIMS, dims) if e < 0]
    return "·".join(up or ["1"]) + ("/" + "·".join(down) if down else "")


TABLE_UNITS = {}    # category -> [(expression, name, factor, dims, offset)]


def table_units(category):
    if category not in TABLE_UNITS:
        rows = []
        spec = TABLES.get(category)
        if category == "Currency":
            spec = "|".join(f"{u.symbol}={CURRENCY_NAMES.get(u.symbol, u.name)}" for u in sorted(
                (u for u in set(SYMBOLS.values()) if u.category == "Currency"), key=lambda u: u.symbol))
        for entry in (spec or "").split("|"):
            if not entry:
                continue
            expr, _, name = entry.partition("=") if "=" in entry else entry.partition(" ")
            u = SYMBOLS.get(expr)
            if u is not None and u.offset:
                rows.append((expr, name, u.factor, u.dims, u.offset))
                continue
            q = Parser(tokenize(expr)).parse()
            rows.append((expr, name, q.value, q.dims, 0.0))
        if category == "Currency":
            TABLE_UNITS.pop(category, None)
            return rows
        TABLE_UNITS[category] = rows
    return TABLE_UNITS[category]


def categories_for(dims):
    out = []
    for cat in CATEGORY_ORDER:
        if cat == "Number":
            continue
        rows = table_units(cat)
        if rows and (same(rows[0][3], dims) or cat == "Fuel economy" and same(rows[0][3], tuple(-d for d in dims))):
            out.append(cat)
    return out


# -- converting --

class Result:
    def __init__(self):
        self.q = None              # the quantity, in SI
        self.absolute = False      # a temperature on its scale, not a difference
        self.target = None         # (text, factor, dims, offset) or a composite [(text, factor)] or a base name
        self.value = None          # the converted number
        self.text = ""             # the result as shown
        self.notes = []
        self.category = None
        self.density = None
        self.hints = []            # the unit text of the input and of the target
        self.error = None


def strip_ingredient(text):
    low = text.lower()
    for name in sorted(INGREDIENTS, key=len, reverse=True):
        m = re.search(r"(?:\bof\s+)?\b" + re.escape(name) + r"\b", low)
        if m:
            return text[:m.start()] + text[m.end():], name
    return text, None


def normalise(text):
    for pattern, repl in PHRASES:
        text = re.sub(r"(?i)\b" + pattern + r"\b", repl, text)
    for sign, code in CURRENCY_SIGNS.items():
        if sign in text:
            text = re.sub(re.escape(sign) + r"\s*(\d[\d.,]*)", r"\1 " + code, text)
            text = text.replace(sign, " " + code + " ")
    return text


def split_target(text):
    """'5 ft to cm' -> ('5 ft', 'cm'); 'in' as the keyword only when both sides make sense."""
    for sep in (r"\s+(?:to|into|as)\s+", r"\s*(?:->|→|=)\s*"):
        parts = re.split(sep, text, flags=re.I)
        if len(parts) >= 2 and parts[-1].strip():
            return " to ".join(parts[:-1]), parts[-1].strip()
    for m in reversed(list(re.finditer(r"\s+in\s+(?=\S)", text))):
        left, right = text[:m.start()], text[m.end():]
        try:
            Parser(tokenize(normalise(left))).parse()
            if right.strip().lower() not in BASES:
                parse_target(right)
            return left, right.strip()
        except ConvError:
            continue
    return text, None


def single_affine(tokens):
    """The unit, when tokens are just [-]number unit and the unit is a temperature scale."""
    toks = tokens[1:] if tokens and tokens[0] == ("op", "-", "-") else tokens
    if len(toks) == 2 and toks[0][0] == "num" and toks[1][0] == "id":
        name = {"C": "degC", "F": "degF", "°C": "degC", "°F": "degF", "R": "degR"}.get(toks[1][1], toks[1][1])
        u = lookup(name)
        if u and u.category == "Temperature":
            return u
    return None


def parse_target(text):
    """A target: a temperature scale, a single expression, or a composite like 'ft in' / 'h min s'."""
    text = normalise(text.strip())
    alone = {"C": "degC", "F": "degF", "°C": "degC", "°F": "degF", "R": "degR"}.get(text, text)
    u = lookup(alone)
    if u is not None and u.category in ("Temperature", "Currency"):
        return ("unit", u.symbol, u.factor, u.dims, u.offset)
    pieces = [p for p in re.split(r"[\s+;,:]+", text) if p]
    if len(pieces) > 1:
        units = [lookup(p) for p in pieces]
        if all(units) and all(same(units[0].dims, x.dims) for x in units):
            return ("composite", [(p, x.factor) for p, x in zip(pieces, units)], units[0].dims)
    q = Parser(tokenize(text)).parse()
    return ("unit", text, q.value, q.dims, 0.0)


def convert_value(q, absolute, factor, dims, offset, density=None):
    """The number of `factor` units in q, with a note when it took a bridge to get there."""
    if same(q.dims, dims):
        if absolute and offset:
            return q.value / factor - offset, None
        return q.value / factor, None
    if same(q.dims, tuple(-d for d in dims)) and q.value:
        return 1 / (q.value * factor), "one is the reciprocal of the other"
    diff = add_dims(dims, q.dims, -1)
    if all(abs(d) < 1e-9 for k, d in enumerate(diff) if DIMS[k] != "rad"):
        k = diff[DIMS.index("rad")]
        return q.value * (2 * math.pi) ** k / factor, "1 cycle counted as 1 revolution"
    vol, mass = SYMBOLS["L"].dims, SYMBOLS["g"].dims
    rho = (density or 1.0) * 1000                               # kg/m³
    if same(q.dims, vol) and same(dims, mass):
        return q.value * rho / factor, None if density else "as water — add an ingredient, e.g. 1 cup flour to g"
    if same(q.dims, mass) and same(dims, vol):
        return q.value / rho / factor, None if density else "as water — add an ingredient, e.g. 200 g sugar to cups"
    raise ConvError(f"can't convert {describe(q.dims)} to {describe(dims)}")


# with no target, the result line shows the usual counterpart
COUNTERPART = {
    "in": "cm", "inch": "cm", "inches": "cm", "ft": "m", "feet": "m", "foot": "m", "yd": "m", "mi": "km",
    "miles": "km", "mile": "km", "nmi": "km", "m": "ft", "cm": "in", "mm": "in", "km": "mi", "lb": "kg", "lbs": "kg",
    "oz": "g", "kg": "lb", "g": "oz", "st": "kg", "degF": "degC", "°F": "degC", "F": "degC", "degC": "degF",
    "°C": "degF", "C": "degF", "K": "degC", "mph": "km/h", "km/h": "mph", "kph": "mph", "kn": "km/h",
    "gal": "L", "L": "gal", "l": "gal", "floz": "mL", "mL": "floz", "ml": "floz", "cup": "mL", "cups": "mL",
    "tbsp": "mL", "tsp": "mL", "impgal": "L", "psi": "bar", "bar": "psi", "hp": "kW", "kW": "hp", "PS": "kW",
    "pk": "kW", "kWh": "MJ", "cal": "J", "kcal": "kJ", "J": "cal", "kJ": "kcal", "acre": "ha", "ha": "acre",
    "ft^2": "m^2", "m^2": "ft^2", "mpg": "L/(100 km)", "L/100km": "mpg", "L/(100 km)": "mpg", "lbf": "N",
    "N": "lbf", "N m": "lbf ft", "Nm": "lbf ft", "lbf ft": "N m", "ftlbf": "J", "atm": "bar", "rpm": "Hz",
    "Hz": "rpm", "deg": "rad", "rad": "deg", "ly": "km", "au": "km", "pc": "ly", "eV": "J", "BTU": "kJ",
    "GB": "GiB", "GiB": "GB", "TB": "TiB", "TiB": "TB", "MB": "MiB", "MiB": "MB", "Mbit/s": "MB/s",
    "Mbps": "MB/s", "MB/s": "Mbit/s", "px": "mm", "point": "mm", "s": "min", "min": "h", "h": "d", "d": "wk",
}
CURRENCY_NAMES = {"EUR": "euro", "USD": "US dollar", "JPY": "Japanese yen", "GBP": "pound sterling",
                  "CHF": "Swiss franc", "SEK": "Swedish krona", "NOK": "Norwegian krone", "DKK": "Danish krone",
                  "PLN": "Polish złoty", "CZK": "Czech koruna", "HUF": "Hungarian forint", "RON": "Romanian leu",
                  "BGN": "Bulgarian lev", "ISK": "Icelandic króna", "TRY": "Turkish lira", "AUD": "Australian dollar",
                  "CAD": "Canadian dollar", "NZD": "New Zealand dollar", "CNY": "Chinese yuan",
                  "HKD": "Hong Kong dollar", "SGD": "Singapore dollar", "INR": "Indian rupee", "IDR": "Indonesian rupiah",
                  "KRW": "South Korean won", "MYR": "Malaysian ringgit", "PHP": "Philippine peso", "THB": "Thai baht",
                  "ILS": "Israeli shekel", "BRL": "Brazilian real", "MXN": "Mexican peso", "ZAR": "South African rand"}


def evaluate(text):
    r = Result()
    raw = text.strip()
    if not raw:
        return r
    try:
        body, density_name = strip_ingredient(raw)
        if density_name:
            r.density = INGREDIENTS[density_name]
            r.notes.append(f"{density_name}: {r.density:g} g/mL")
        left, target = split_target(body)
        left = normalise(left)
        tokens = tokenize(left)
        q = Parser(tokens).parse()
        aff = single_affine(tokens)
        if aff:
            n = tokens[-2][1] * (-1 if tokens[0] == ("op", "-", "-") else 1)
            q = Q((n + aff.offset) * aff.factor, aff.dims, (aff,), False)
            r.absolute = True
        elif any(u.offset for u in q.units):
            r.notes.append("read as a temperature difference")
        r.q = q
        unit_text = re.sub(r"^\s*[-+]?(?:\d[\d.,_]*(?:[eE][-+]?\d+)?|\.\d+)\s*", "", left).strip()
        r.hints = [h for h in (unit_text, target and normalise(target.strip())) if h]
        if target and target.strip().lower() in BASES:
            r.target = ("base", target.strip().lower())
            r.category = "Number"
            r.text = in_base(q, BASES[target.strip().lower()])
            return r
        if target:
            t = parse_target(target)
            r.target = t
            if t[0] == "composite":
                r.text = composite(q, t[1])
            else:
                value, note = convert_value(q, r.absolute, t[2], t[3], t[4], r.density)
                r.value = value
                if note:
                    r.notes.append(note)
                r.text = f"{fmt(value)} {pretty(t[1])}"
        r.category = pick_category(r)
        if not target:
            if r.category == "Number" or (same(q.dims, NONE) and not q.units):
                r.category = "Number" if float(q.value).is_integer() else "Ratio"
                r.text = fmt(q.value)
            elif not r.category:
                r.text = f"{fmt(q.value)} {describe(q.dims)}"
            elif r.category == "Ratio":
                r.text = fmt(q.value)
            else:
                default = COUNTERPART.get(unit_text) or COUNTERPART.get(unit_text.replace(" ", ""))
                if not default:              # else the SI unit of its kind, unless that's what you wrote
                    si = [row[0] for row in table_units(r.category) if abs(row[2] - 1) < 1e-12 and not row[4]]
                    default = si[0] if si and si[0] != unit_text and r.category not in ("Currency",) else None
                if default:
                    t = parse_target(default)
                    value, note = convert_value(q, r.absolute, t[2], t[3], t[4], r.density)
                    r.text = f"{fmt(value)} {pretty(t[1])}"
    except ConvError as e:
        r.error = str(e)
    except (OverflowError, ZeroDivisionError, ValueError) as e:
        r.error = str(e)
    return r


SPECIAL = ("Torque", "Typography", "Radioactivity", "Radiation dose", "Angular speed")
BY_HINT = ("Torque",)        # its units belong to other kinds (lbf, ft), so it's recognised by the text


def pick_category(r):
    """Which table to show: a special kind when you wrote one of its units (lbf ft is torque, px is
    typography), else the kind of the units you wrote, else the first kind with these dimensions."""
    q = r.q
    if q.bare and same(q.dims, NONE):
        return "Number" if float(q.value).is_integer() else "Ratio"
    dims = r.target[3] if r.target and r.target[0] == "unit" else q.dims
    if r.target and r.target[0] == "composite":
        dims = r.target[2]
    cats = categories_for(dims)
    if not cats:
        return None
    names = {u.category for u in q.units}
    for cat in cats:
        exprs = {row[0] for row in table_units(cat)}
        if cat in SPECIAL and (cat in names or cat in BY_HINT and any(h in exprs for h in r.hints)):
            return cat
    for cat in cats:
        if cat in names and cat not in SPECIAL:
            return cat
    for cat in cats:
        if any(h in {row[0] for row in table_units(cat)} for h in r.hints):
            return cat
    return cats[0]


def composite(q, parts):
    """5.9 ft as 'ft in' -> 5 ft 10.8 in: whole numbers of each unit but the last."""
    sign = "-" if q.value < 0 else ""
    rest = abs(q.value)
    values = []
    for k, (name, f) in enumerate(parts):
        if k < len(parts) - 1:
            n = math.floor(rest / f + 1e-9)
            values.append(n)
            rest -= n * f
        else:
            last = float(fmt(round(rest / f, 9), plain=True))
            values.append(last)
    for k in range(len(values) - 1, 0, -1):            # 5 ft 12 in -> 6 ft 0 in
        if values[k] * parts[k][1] >= parts[k - 1][1] * (1 - 1e-12):
            values[k] -= parts[k - 1][1] / parts[k][1]
            values[k - 1] += 1
    shown = [f"{fmt(v)}{'' if n in '°′″' else ' '}{pretty(n)}" for v, (n, _) in zip(values, parts) if v]
    shown = shown or [f"0 {pretty(parts[-1][0])}"]
    return sign + " ".join(shown)


ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
         (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def roman(n):
    if not 0 < n < 4000:
        return "—"
    out = ""
    for v, s in ROMAN:
        while n >= v:
            out += s
            n -= v
    return out


def in_base(q, base):
    if not same(q.dims, NONE) or not float(q.value).is_integer():
        raise ConvError("only whole plain numbers have a base")
    n = int(q.value)
    sign, a = ("-" if n < 0 else ""), abs(n)
    if base == 0:
        return roman(n)
    return sign + {16: "0x" + format(a, "X"), 2: "0b" + format(a, "b"), 8: "0o" + format(a, "o"), 10: str(a)}[base]


def number_rows(q):
    n = int(q.value)
    a = abs(n)
    sign = "-" if n < 0 else ""
    group = lambda s, k: " ".join(s[max(0, i - k):i] for i in range(len(s), 0, -k)[::-1])
    return [(f"{n:,}", "decimal"), (sign + "0x" + format(a, "X"), "hexadecimal"),
            (sign + "0o" + format(a, "o"), "octal"), (sign + "0b" + group(format(a, "b"), 4), "binary"),
            (roman(n), "Roman numerals"), (f"{n:.6e}", "scientific"),
            (f"{a.bit_length()} bits, {(a.bit_length() + 7) // 8 or 1} bytes", "size")]


def pretty(expr):
    s = re.sub(r"\^(-?\d+)", lambda m: m[1].translate(SUPER_OUT), expr)
    s = re.sub(r"\b([A-Za-z]+)(\d+)\b", lambda m: m[0] if lookup(m[0]) else m[1] + m[2].translate(SUPER_OUT), s)
    s = s.replace("degC", "°C").replace("degF", "°F").replace("degR", "°R").replace("*", "·")
    s = re.sub(r"(?<=\w) (?=\w)", "·", s) if not re.search(r"\d", s.split()[0] if s.split() else "") else s
    return s


def fmt(x, plain=False, digits=None):
    """A number to `digits` significant digits; scientific with ×10ⁿ when very large or small."""
    digits = digits or DIGITS
    if x is None:
        return ""
    if isinstance(x, float) and (math.isinf(x) or math.isnan(x)):
        return "∞" if x > 0 else "-∞" if math.isinf(x) else "undefined"
    if x == 0:
        return "0"
    a = abs(x)
    if 1e-4 <= a < 1e15:
        magnitude = math.floor(math.log10(a))
        decimals = max(0, digits - 1 - magnitude)
        if magnitude >= digits:
            x = round(x, digits - 1 - magnitude)       # 9,460,730,000,000 rather than every digit
        s = f"{x:.{decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        if plain:
            return s
        whole, dot, frac = s.partition(".")
        neg = whole.startswith("-")
        whole = whole.lstrip("-")
        whole = f"{int(whole):,}" if len(whole) > 4 else whole
        return ("-" if neg else "") + whole + dot + frac
    mant, exp = f"{x:.{digits - 1}e}".split("e")
    mant = mant.rstrip("0").rstrip(".") if "." in mant else mant
    if plain:
        return f"{mant}e{int(exp)}"
    return f"{mant}×10{str(int(exp)).translate(SUPER_OUT)}"


def also(r):
    """Extra readings worth a line: 1.8 m is 5 ft 10.9 in, 12345 s is 3 h 25 min 45 s."""
    q = r.q
    if not q or r.error or r.target and r.target[0] != "unit":
        return []
    out = []
    if r.category == "Time" and abs(q.value) >= 60:
        parts = [("yr", 31556952), ("d", 86400), ("h", 3600), ("min", 60), ("s", 1)]
        used = [p for p in parts if abs(q.value) >= p[1]][:4]
        out.append(composite(q, used))
    elif r.category == "Length" and 0.3 <= abs(q.value) < 3:
        out.append(composite(q, [("ft", 0.3048), ("in", 0.0254)]))
    elif r.category == "Mass" and 0.5 <= abs(q.value) < 300:
        out.append(composite(q, [("st", 6.35029318), ("lb", 0.45359237)]) if abs(q.value) >= 6.35 else
                   composite(q, [("lb", 0.45359237), ("oz", 0.028349523125)]))
    elif r.density and same(q.dims, SYMBOLS["L"].dims) and r.category != "Mass":
        out.append(f"{fmt(q.value * r.density * 1e6)} g")
    elif r.density and same(q.dims, SYMBOLS["g"].dims) and r.category != "Volume":
        ml = q.value / r.density * 1000
        out.append(f"{fmt(ml)} mL  ·  {fmt(ml / 236.5882365)} cups")
    elif r.category == "Angle" and not r.target:
        out.append(composite(q, [("°", math.pi / 180), ("′", math.pi / 10800), ("″", math.pi / 648000)]))
    source = set(re.findall(r"[^\W\d]+|[°′″]", r.hints[0])) if r.hints else set()
    out = [s for s in out if not set(re.findall(r"[^\W\d]+|[°′″]", s)) <= source]   # not what you typed
    return [f"= {s}" for s in out]


# -- currency --

class Rates:
    def __init__(self):
        self.date = None
        self.busy = False
        self.error = None

    def fetch(self):
        self.busy = True
        try:
            req = urllib.request.Request(RATES_URL, headers={"User-Agent": "unit_tui"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
                root = ET.fromstring(f.read())
            for cube in root.iter():
                if cube.get("time"):
                    self.date = cube.get("time")
                if cube.get("currency"):
                    code, rate = cube.get("currency"), float(cube.get("rate"))
                    u = Unit(code, code, 1 / rate, SYMBOLS["EUR"].dims, category="Currency")
                    SYMBOLS[code] = u
                    NAMES.setdefault(code.lower(), u)
            self.error = None
        except (OSError, ET.ParseError, ValueError) as e:
            self.error = f"couldn't fetch rates: {getattr(e, 'reason', e)}"
        finally:
            self.busy = False


def wants_rates(text):
    words = set(re.findall(r"[A-Za-z]{3}", text))
    return any(w.upper() in ISO_CODES and w not in SYMBOLS and w.lower() not in NAMES for w in words) or \
        any(s in text for s in CURRENCY_SIGNS if s != "€")


# -- the app --

def cwidth(text):
    if text.isascii():
        return len(text)
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = cwidth(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


EXAMPLES = ["5 ft 11 in to cm", "100 km/h in mph", "72 F", "1 cup flour to g", "8 L/100km to mpg",
            "1.5 GiB to MB", "3 h 20 min / 4", "0xff to bin", "50 lbf ft to N m", "1 kWh to MJ", "20 °C to F",
            "12°30'15\" to deg", "1 ly to km", "60 mi / 45 min to km/h", "2 tsp to mL", "100 usd to eur"]
HELP = [
    ("Writing", None),
    ("5 ft 11 in to cm", "numbers and units; `to`, `in`, `as`, `->` or `=` before the target"),
    ("72 F   -40 C", "a temperature on its own; C and F inside other units are coulomb and farad"),
    ("km/h  kg m/s^2  J/(kg K)", "compound units; side by side multiplies and goes before /"),
    ("m2  cm³  s^-1", "powers, also as superscripts"),
    ("1/2 cup  3 ½ tsp  1,5 kg", "fractions and decimal commas"),
    ("(3 m + 40 cm) * 2", "arithmetic: + - * / ^ and brackets"),
    ("1 cup flour to g", "an ingredient turns volume into mass (about 70 known)"),
    ("8 L/100km to mpg", "reciprocal units convert into each other"),
    ("6.2 ft to ft in", "several units: whole numbers of all but the last (h min s, lb oz, st lb)"),
    ("255 to hex", "bases: hex, bin, oct, dec, roman; 0xff 0b1010 0o17 as input"),
    ("100 usd to eur", "currencies: ECB reference rates, fetched the first time you use one"),
    ("Keys", None),
    ("↑↓ PgUp PgDn", "move in the table"),
    ("⏎", "copy the selected row's number (⇧⏎ or ^Y copies it with its unit)"),
    ("^T", "make the selected row the target"),
    ("Tab ⇧Tab", "next and previous kind of unit"),
    ("^S", "swap: convert the result back"),
    ("^P", "precision: 4, 6, 8, 10 or 15 significant digits"),
    ("^U ^W  ← → Home End", "clear the line, delete a word, move in it"),
    ("^E", "an example"),
    ("Esc", "clear the line; on an empty line, quit"),
]


class App:
    def __init__(self, stdscr, text=""):
        self.stdscr = stdscr
        self.buf = list(text)
        self.cur = len(self.buf)
        self.sel = 0
        self.top = 0
        self.cat_top = 0
        self.browse = "Length"
        self.help = False
        self.example = 0
        self.rates = Rates()
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.result = None
        self.last_text = None

    @property
    def text(self):
        return "".join(self.buf)

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h or x < 0:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    def evaluate(self):
        text = self.text
        if text == self.last_text and self.result is not None:
            return self.result
        if wants_rates(text) and not self.rates.busy and self.rates.date is None and self.rates.error is None:
            threading.Thread(target=self.rates.fetch, daemon=True).start()
            self.rates.busy = True
        r = evaluate(text) if text.strip() else None
        if r and r.error and self.rates.busy and wants_rates(text):
            r.error = "fetching exchange rates from the ECB…"
        if r is not None and r.category != (self.result.category if self.result else None):
            self.sel = None
        self.result, self.last_text = r, text
        if r and r.category:
            self.browse = r.category
        return r

    def rows(self):
        """(value text, unit text, name, number, is target) for the table."""
        r = self.result
        cat = self.browse
        if r is None or r.error or r.q is None:
            if cat == "Number":
                return [(v, "", n, None, False) for v, n in number_rows(Q(255.0))]
            rows = table_units(cat)
            if not rows:
                return []
            first = rows[0]                    # nothing typed: one of the first unit
            absolute = bool(first[4]) or cat == "Temperature"
            q = Q((1 + first[4]) * first[2], first[3])
            out = []
            for expr, name, factor, dims, offset in rows:
                v, _ = convert_value(q, absolute, factor, dims, offset)
                out.append((fmt(v), pretty(expr), name, v, False))
            return out
        q = r.q
        if r.category == "Number":
            return [(v, "", n, None, bool(r.target and r.target[0] == "base" and n.lower().startswith(r.target[1][:3])))
                    for v, n in number_rows(q)]
        out = []
        target = r.target[1] if r.target and r.target[0] == "unit" else None
        for expr, name, factor, dims, offset in table_units(cat):
            try:
                v, _ = convert_value(q, r.absolute, factor, dims, offset, r.density)
            except ConvError:
                continue
            out.append((fmt(v), pretty(expr), name, v, expr == target or (target and lookup(target) is not None
                                                                          and lookup(target) is SYMBOLS.get(expr))))
        return out

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        r = self.evaluate()
        self.put(0, 0, " Units ", curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE)
        right = f"{DIGITS} digits"
        if self.browse == "Currency" or (r and r.category == "Currency"):
            right = (f"ECB rates of {self.rates.date}" if self.rates.date else
                     "fetching rates…" if self.rates.busy else self.rates.error or "rates not fetched") + " · " + right
        self.put(0, w - cwidth(right) - 1, right, curses.color_pair(5))
        if self.help:
            self.draw_help(h, w)
            self.draw_footer(h - 1, w)
            s.refresh()
            return
        # result first, so the cursor ends on the input line
        y = 4
        if r is None:
            self.put(y, 2, "type a conversion, e.g. " + EXAMPLES[self.example % len(EXAMPLES)], curses.color_pair(5))
        elif r.error:
            self.put(y, 2, r.error, curses.color_pair(4 if not r.error.startswith("fetching") else 3))
        else:
            extra = also(r) + [f"({n})" for n in r.notes]
            main = r.text or (extra.pop(0)[2:] if extra and extra[0].startswith("= ") else "")
            if main:
                self.put(y, 2, "= " + main, curses.color_pair(2) | curses.A_BOLD)
            self.put(y + 1, 4, "   ".join(extra), curses.color_pair(5))
        self.draw_body(7, h, w)
        self.draw_footer(h - 1, w)
        # the input line
        self.put(2, 0, " › ", curses.color_pair(1) | curses.A_BOLD)
        room = w - 5
        start = max(0, self.cur - room + 1)
        visible = self.text[start:start + room]
        self.put(2, 3, visible, curses.A_BOLD)
        self.put(3, 3, "─" * (w - 5), curses.color_pair(5))
        try:
            s.move(2, 3 + cwidth(self.text[start:self.cur]))
        except curses.error:
            pass
        s.refresh()

    def draw_body(self, y0, h, w):
        height = h - 1 - y0
        cats = CATEGORY_ORDER
        ci = cats.index(self.browse) if self.browse in cats else 0
        if ci < self.cat_top:
            self.cat_top = ci
        elif ci >= self.cat_top + height:
            self.cat_top = ci - height + 1
        for k, cat in enumerate(cats[self.cat_top:self.cat_top + height]):
            here = cat == self.browse
            self.put(y0 + k, 1, " " + cat, (curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD) if here
                     else curses.color_pair(5), CAT_W - 1)
        x0 = CAT_W + 2
        self.put(y0 - 1, x0, self.browse, curses.color_pair(1) | curses.A_BOLD)
        rows = self.rows()
        if not rows:
            self.put(y0, x0, "nothing to show", curses.color_pair(5))
            return
        if self.sel is None:
            marked = [k for k, row in enumerate(rows) if row[4]]
            self.sel = marked[0] if marked else 0
        self.sel = max(0, min(self.sel, len(rows) - 1))
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + height:
            self.top = self.sel - height + 1
        self.top = max(0, min(self.top, len(rows) - height))
        val_w = min(24, max(cwidth(row[0]) for row in rows) + 1)
        unit_w = min(14, max(cwidth(row[1]) for row in rows) + 2)
        for k, (value, unit, name, _, target) in enumerate(rows[self.top:self.top + height]):
            y = y0 + k
            here = self.top + k == self.sel
            base = curses.A_REVERSE if here else 0
            self.put(y, x0, " " * (w - x0 - 1), base)
            self.put(y, x0, value.rjust(val_w), base | (curses.A_BOLD | curses.color_pair(2) if target else 0), val_w)
            self.put(y, x0 + val_w + 1, unit, base | curses.A_BOLD, unit_w)
            self.put(y, x0 + val_w + 1 + unit_w, name, base | curses.color_pair(5), w - x0 - val_w - unit_w - 4)
            if target:
                self.put(y, w - 2, "◀", base | curses.color_pair(2) | curses.A_BOLD, 1)

    def draw_help(self, h, w):
        for k, (keys, what) in enumerate(HELP[:h - 3]):
            y = 2 + k
            if what is None:
                self.put(y, 1, keys, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.put(y, 3, keys, curses.A_BOLD, 26)
                self.put(y, 30, what, curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = ["↑↓ table", "⏎ copy", "^T target", "Tab kind", "^S swap", "^P digits", "^E example", "F1 help",
                "Esc quit" if not self.text else "Esc clear"]
        if self.help:
            keys = ["Esc back"]
        while len(keys) > 2 and cwidth("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(y, 0, " " + "  ".join(keys), curses.color_pair(5))

    # -- actions --

    def set_text(self, text):
        self.buf = list(text)
        self.cur = len(self.buf)
        self.sel = None

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {what}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))

    def selected(self):
        rows = self.rows()
        return rows[self.sel] if rows and self.sel is not None and self.sel < len(rows) else None

    def copy_row(self, with_unit):
        row = self.selected()
        if not row or not row[0]:
            return
        r = self.result
        if row[3] is not None:
            number = fmt(row[3], plain=True)
        else:
            number = row[0].replace(",", "") if r and r.category != "Number" else row[0]
        text = f"{number} {row[1].replace('·', ' ')}".strip() if with_unit else number
        self.copy(text, text)

    def make_target(self):
        row = self.selected()
        r = self.result
        if not row or not r or r.error or r.q is None or r.category == "Number":
            return
        left, _ = split_target(strip_ingredient(self.text)[0])
        ingredient = strip_ingredient(self.text)[1]
        unit = row[1].replace("·", " ").replace("°C", "C").replace("°F", "F").replace("°R", "degR")
        unit = re.sub(r"[⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^" + m[0].translate(SUPERSCRIPTS), unit)
        self.set_text(f"{left.strip()}{' ' + ingredient if ingredient else ''} to {unit}")

    def swap(self):
        r = self.result
        if not r or r.error or not r.target or r.target[0] != "unit" or r.value is None:
            self.say("swap needs a conversion with a target, like 5 ft to cm")
            return
        body, ingredient = strip_ingredient(self.text)
        left, target = split_target(body)
        unit = re.sub(r"\s+", " ", re.sub(r"[-+]?\d[\d.,_]*(?:[eE][-+]?\d+)?", " ", left)).strip() or left
        self.set_text(f"{fmt(r.value, plain=True)} {target}{' ' + ingredient if ingredient else ''} to {unit}")

    def cycle_category(self, step):
        cats = CATEGORY_ORDER
        i = (cats.index(self.browse) + step) % len(cats) if self.browse in cats else 0
        self.browse = cats[i]
        if self.browse == "Number":
            self.set_text("255")
        elif self.browse == "Currency":
            self.set_text("100 USD to EUR")
        elif self.browse == "Ratio":
            self.set_text("50 %")
        else:
            rows = table_units(self.browse)
            unit = rows[0][0]
            self.set_text(f"1 {'C' if unit == 'degC' else unit}" if self.browse != "Temperature" else "20 C")
        self.sel = None

    def handle(self, key):
        global DIGITS
        if self.help:
            if key in ("\x1b", "q", curses.KEY_F1, "\x07", "\n", "\r"):
                self.help = False
            return True
        if isinstance(key, str):
            if key in ("\n", "\r"):
                self.copy_row(False)
            elif key == "\x1b":
                if not self.buf:
                    return False
                self.set_text("")
            elif key in ("\x7f", "\b"):
                if self.cur:
                    del self.buf[self.cur - 1]
                    self.cur -= 1
            elif key == "\x15":                      # ^U
                self.set_text("")
            elif key == "\x17":                      # ^W
                k = self.cur
                while k and self.buf[k - 1] == " ":
                    k -= 1
                while k and self.buf[k - 1] != " ":
                    k -= 1
                del self.buf[k:self.cur]
                self.cur = k
            elif key == "\x01":                      # ^A
                self.cur = 0
            elif key == "\x05":                      # ^E
                self.set_text(EXAMPLES[self.example % len(EXAMPLES)])
                self.example += 1
            elif key == "\x13":                      # ^S
                self.swap()
            elif key == "\x14":                      # ^T
                self.make_target()
            elif key == "\x19":                      # ^Y
                self.copy_row(True)
            elif key == "\x10":                      # ^P
                DIGITS = PRECISIONS[(PRECISIONS.index(DIGITS) + 1) % len(PRECISIONS)] if DIGITS in PRECISIONS else 6
                self.result = None
                self.say(f"{DIGITS} significant digits")
            elif key == "\x07":                      # ^G
                self.help = True
            elif key == "\t":
                self.cycle_category(1)
            elif key == "\x04":                      # ^D
                return False
            elif key.isprintable():
                self.buf.insert(self.cur, key)
                self.cur += 1
            return True
        rows = len(self.rows())
        h = self.stdscr.getmaxyx()[0]
        page = max(1, h - 9)
        if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE) and rows:
            step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page}[key]
            self.sel = max(0, min(rows - 1, (self.sel or 0) + step))
        elif key == curses.KEY_LEFT:
            self.cur = max(0, self.cur - 1)
        elif key == curses.KEY_RIGHT:
            self.cur = min(len(self.buf), self.cur + 1)
        elif key == curses.KEY_HOME:
            self.cur = 0
        elif key == curses.KEY_END:
            self.cur = len(self.buf)
        elif key in (curses.KEY_BACKSPACE,):
            if self.cur:
                del self.buf[self.cur - 1]
                self.cur -= 1
        elif key == curses.KEY_DC:
            if self.cur < len(self.buf):
                del self.buf[self.cur]
        elif key == curses.KEY_BTAB:
            self.cycle_category(-1)
        elif key == curses.KEY_F1:
            self.help = True
        elif key == curses.KEY_ENTER:
            self.copy_row(False)
        elif key in (getattr(curses, "KEY_SENTER", -2), 343):
            self.copy_row(True)
        return True


def run(stdscr, text):
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, categories
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # the result
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.raw()                                   # ^S, ^Y and ^C reach us instead of the terminal
    stdscr.keypad(True)
    app = App(stdscr, text)
    while True:
        curses.curs_set(0 if app.help else 1)
        if not app.rates.busy and app.result and app.result.error and app.result.error.startswith("fetching"):
            app.result = None              # the rates are in: work it out again
        app.draw()
        stdscr.timeout(200 if app.rates.busy else -1)
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        if key == "\x03":                              # ^C under raw()
            return
        if key == curses.KEY_RESIZE:
            continue
        if not app.handle(key):
            return


def print_once(text, table):
    if wants_rates(text):
        rates = Rates()
        rates.fetch()
        if rates.error:
            print(rates.error, file=sys.stderr)
    r = evaluate(text)
    if r.error:
        print(r.error, file=sys.stderr)
        return 1
    tty = sys.stdout.isatty()
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if tty else ("", "", "")
    extra = also(r) + [f"({n})" for n in r.notes]
    main = r.text or (extra.pop(0)[2:] if extra and extra[0].startswith("= ") else "")
    if main:
        print(f"{bold}{main}{reset}")
    for line in extra:
        print(f"{dim}{line}{reset}")
    if table or not main:
        if r.category == "Number":
            rows = [(v, "", n) for v, n in number_rows(r.q)]
        elif r.category:
            rows = []
            for expr, name, factor, dims, offset in table_units(r.category):
                try:
                    v, _ = convert_value(r.q, r.absolute, factor, dims, offset, r.density)
                    rows.append((fmt(v), pretty(expr), name))
                except ConvError:
                    pass
        else:
            rows = []
        if rows:
            vw = max(len(v) for v, _, _ in rows)
            uw = max(cwidth(u) for _, u, _ in rows)
            print(f"{dim}{r.category}{reset}")
            for v, u, n in rows:
                print(f"  {v:>{vw}}  {fit(u, uw)}  {dim}{n}{reset}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Convert units: type what you'd say, like '5 ft 11 in to cm'.")
    parser.add_argument("expression", nargs="*", help="a conversion to start with (or to print, with --once)")
    parser.add_argument("--once", action="store_true", help="print the result and exit")
    parser.add_argument("--table", action="store_true", help="with --once, also print every unit of its kind")
    args = parser.parse_args()
    text = " ".join(args.expression)
    if args.once:
        if not text:
            parser.error("--once needs an expression")
        raise SystemExit(print_once(text, args.table))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, text)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
