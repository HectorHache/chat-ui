"""
household_convert — Currency & unit converter.

Currency uses the free Frankfurter API (ECB daily rates, no key).
Units (length/weight/volume/temperature) are converted locally.
"""

import asyncio
import json
import urllib.parse
import urllib.request

_CURRENCY_ALIASES = {
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "€": "EUR",
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD", "$": "USD",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "£": "GBP",
    "chf": "CHF", "swiss franc": "CHF",
    "yen": "JPY", "jpy": "JPY", "¥": "JPY",
    "cad": "CAD", "canadian dollar": "CAD",
    "aud": "AUD", "australian dollar": "AUD",
    "sek": "SEK", "nok": "NOK", "dkk": "DKK", "pln": "PLN", "czk": "CZK",
    "mxn": "MXN", "ars": "ARS", "clp": "CLP", "brl": "BRL", "pen": "PEN",
    "try": "TRY", "inr": "INR", "cny": "CNY", "krw": "KRW", "hkd": "HKD",
}

# conversion to SI base: m, kg, l, °C
_UNITS = {
    # length -> meters
    "m": ("m", 1.0), "meter": ("m", 1.0), "meters": ("m", 1.0), "metre": ("m", 1.0),
    "km": ("m", 1000.0), "kilometer": ("m", 1000.0), "kilometers": ("m", 1000.0),
    "cm": ("m", 0.01), "centimeter": ("m", 0.01),
    "mm": ("m", 0.001), "millimeter": ("m", 0.001),
    "ft": ("m", 0.3048), "foot": ("m", 0.3048), "feet": ("m", 0.3048),
    "in": ("m", 0.0254), "inch": ("m", 0.0254), "inches": ("m", 0.0254),
    "yd": ("m", 0.9144), "yard": ("m", 0.9144), "yards": ("m", 0.9144),
    "mi": ("m", 1609.344), "mile": ("m", 1609.344), "miles": ("m", 1609.344),
    # weight -> kg
    "kg": ("kg", 1.0), "kilogram": ("kg", 1.0), "kilograms": ("kg", 1.0),
    "g": ("kg", 0.001), "gram": ("kg", 0.001), "grams": ("kg", 0.001),
    "mg": ("kg", 1e-6), "milligram": ("kg", 1e-6),
    "lb": ("kg", 0.45359237), "lbs": ("kg", 0.45359237), "pound": ("kg", 0.45359237), "pounds": ("kg", 0.45359237),
    "oz": ("kg", 0.028349523125), "ounce": ("kg", 0.028349523125), "ounces": ("kg", 0.028349523125),
    "stone": ("kg", 6.35029318), "st": ("kg", 6.35029318),
    # volume -> liters
    "l": ("l", 1.0), "liter": ("l", 1.0), "liters": ("l", 1.0), "litre": ("l", 1.0), "litres": ("l", 1.0),
    "ml": ("l", 0.001), "milliliter": ("l", 0.001), "millilitre": ("l", 0.001),
    "cl": ("l", 0.01), "centiliter": ("l", 0.01),
    "dl": ("l", 0.1), "deciliter": ("l", 0.1),
    "gal": ("l", 3.785411784), "gallon": ("l", 3.785411784), "gallons": ("l", 3.785411784),
    "us gal": ("l", 3.785411784),
    "qt": ("l", 0.946352946), "quart": ("l", 0.946352946),
    "pt": ("l", 0.473176473), "pint": ("l", 0.473176473), "pints": ("l", 0.473176473),
    "cup": ("l", 0.2365882365), "cups": ("l", 0.2365882365),
    "tbsp": ("l", 0.0147867648), "tablespoon": ("l", 0.0147867648), "tablespoons": ("l", 0.0147867648),
    "tsp": ("l", 0.00492892159), "teaspoon": ("l", 0.00492892159), "teaspoons": ("l", 0.00492892159),
}


def _cur(code: str) -> str:
    code = (code or "").strip().upper()
    return _CURRENCY_ALIASES.get(code.lower(), code)


def _norm_unit(u: str) -> tuple | None:
    u = (u or "").strip().lower().replace(" ", "").replace(".", "")
    return _UNITS.get(u)


class Tools:
    async def currency_convert(self, amount: float, from_currency: str, to_currency: str) -> str:
        """
        Convert an amount between currencies using up-to-date exchange rates.

        Use for "how much is 50 dollars in euros", shopping abroad, travel.

        :param amount: The amount to convert.
        :param from_currency: Source currency (name or code, e.g. EUR, Euro,
            USD, dollar, GBP).
        :param to_currency: Target currency (name or code).
        :return: The converted amount with rate and date.
        """
        try:
            amount = float(amount)
        except Exception:
            return "Please provide a numeric amount."
        src = _cur(from_currency)
        dst = _cur(to_currency)
        if src == dst:
            return f"{amount:,.2f} {src} is {amount:,.2f} {dst} (same currency)."
        try:
            url = "https://api.frankfurter.app/latest?" + urllib.parse.urlencode(
                {"from": src, "to": dst}
            )
            data = await asyncio.to_thread(self._get_json, url)
            rate = data.get("rates", {}).get(dst)
            if not rate:
                return f"Could not find a rate for {src} → {dst}. The currency code may be unknown."
            result = amount * float(rate)
            date = data.get("date", "latest")
            return (
                f"**{amount:,.2f} {src} = {result:,.2f} {dst}**\n"
                f"(rate {float(rate):.4f} on {date}, European Central Bank)"
            )
        except Exception as e:
            return f"Currency service unavailable ({e}). Estimate the conversion yourself and note it is approximate."

    async def unit_convert(self, amount: float, from_unit: str, to_unit: str) -> str:
        """
        Convert between everyday units (length, weight, volume).

        Use for "how many kg is 10 pounds", "6 feet in cm", "2 cups in ml",
        recipes, luggage, furniture, etc. Temperature (°C/°F) is supported
        with c, f, k units.

        :param amount: The numeric amount.
        :param from_unit: Source unit (e.g. kg, lb, ft, cm, cups, ml, °C).
        :param to_unit: Target unit (e.g. g, kg, m, inches, °F).
        :return: The converted value.
        """
        try:
            amount = float(amount)
        except Exception:
            return "Please provide a numeric amount."
        fu = (from_unit or "").strip().lower()
        tu = (to_unit or "").strip().lower()

        temp_map = {"c": "c", "°c": "c", "celsius": "c", "centigrade": "c",
                    "f": "f", "°f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}
        if fu in temp_map and tu in temp_map:
            a, b = temp_map[fu], temp_map[tu]
            if a == b:
                return f"{amount:,.2f}°{a.upper()} = {amount:,.2f}°{b.upper()}."
            if a == "c" and b == "f":
                return f"**{amount:,.1f} °C = {amount * 9 / 5 + 32:,.1f} °F**"
            if a == "c" and b == "k":
                return f"**{amount:,.1f} °C = {amount + 273.15:,.1f} K**"
            if a == "f" and b == "c":
                return f"**{amount:,.1f} °F = {(amount - 32) * 5 / 9:,.1f} °C**"
            if a == "f" and b == "k":
                return f"**{amount:,.1f} °F = {(amount - 32) * 5 / 9 + 273.15:,.1f} K**"
            if a == "k" and b == "c":
                return f"**{amount:,.1f} K = {amount - 273.15:,.1f} °C**"
            if a == "k" and b == "f":
                return f"**{amount:,.1f} K = {(amount - 273.15) * 9 / 5 + 32:,.1f} °F**"

        src = _norm_unit(fu)
        dst = _norm_unit(tu)
        if not src or not dst:
            return (
                f"Unknown unit: '{from_unit}' or '{to_unit}'. Supported: length (m, cm, km, ft, in, mi), "
                "weight (kg, g, lb, oz), volume (l, ml, cups, tbsp, tsp, gal), temperature (°C, °F, K)."
            )
        if src[0] != dst[0]:
            return f"Cannot convert {from_unit} (a {src[0]} unit) to {to_unit} (a {dst[0]} unit)."
        base = amount * src[1]
        result = base / dst[1]
        return f"**{amount:,.2f} {from_unit} = {result:,.2f} {to_unit}**"

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "Chat-hache-household/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
