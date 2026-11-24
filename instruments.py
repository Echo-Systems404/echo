"""Local instruments: the computer's clock, a calendar, and a scientific calculator.

Everything runs on this machine with Python's standard library. Nothing here
touches the network or your personal calendar. The calculator parses math
itself and never runs the expression as code. Plain arithmetic stays exact,
so 0.1 + 0.2 is 0.3 and very large whole numbers don't lose digits.
"""

import ast
import calendar as month_grid
import math
import operator
import statistics
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

MAX_EXPRESSION = 1000
MAX_BITS = 200000  # about 60,000 digits
MAX_COUNTING = 5000  # largest n for factorial, comb, perm
MAX_SHOWN_BITS = 600  # beyond this, show scientific notation


# -- clock -----------------------------------------------------------------

def clock():
    """The current date and time from the system clock."""
    now = datetime.now().astimezone()
    return "\n".join([
        f"Date: {now.strftime('%A, %B')} {now.day}, {now.year}",
        f"Time: {now.strftime('%H:%M:%S')} ({now.strftime('%I:%M %p').lstrip('0')})",
        f"Timezone: {now.tzname()} (UTC{now.strftime('%z')})",
        f"ISO: {now.isoformat(timespec='seconds')}",
        f"Unix: {int(now.timestamp())}",
        f"Day of year: {now.timetuple().tm_yday}, ISO week: {now.isocalendar()[1]}",
    ])


# -- calendar --------------------------------------------------------------

def _day(text):
    if text is None or str(text).strip().lower() == "today":
        return Date.today()
    return Date.fromisoformat(str(text).strip())


def _long(day):
    return f"{day.strftime('%A, %B')} {day.day}, {day.year} ({day.isoformat()})"


def calendar_view(month=None, date=None, add_days=None, until=None):
    """A month grid, one date's weekday, date + days, or days between dates."""
    try:
        if add_days is not None:
            start = _day(date)
            return f"{_long(start)} {int(add_days):+d} days = {_long(start + timedelta(days=int(add_days)))}"
        if until is not None:
            start, end = _day(date), _day(until)
            days = (end - start).days
            weeks, extra = divmod(abs(days), 7)
            return f"From {_long(start)} to {_long(end)}: {days} days ({weeks} weeks, {extra} days)"
        if date is not None:
            day = _day(date)
            return f"{_long(day)}, day {day.timetuple().tm_yday} of {day.year}"
        today = Date.today()
        year, number = (int(p) for p in str(month).split("-")) if month else (today.year, today.month)
        grid = month_grid.TextCalendar(month_grid.SUNDAY).formatmonth(year, number).rstrip()
        return f"{grid}\n\nToday: {_long(today)}"
    except (ValueError, TypeError, OverflowError, IndexError):
        return "Error: use dates like 2026-11-23, months like 2026-11, and whole numbers of days."


# -- calculator ------------------------------------------------------------

def _whole(value):
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError("this needs a whole number")


def _counting(value):
    number = _whole(value)
    if abs(number) > MAX_COUNTING:
        raise OverflowError
    return number


def _snap(value, angle):
    """Remove floating-point dust, so sin(pi) is 0 and cos(pi/3) is 0.5."""
    if abs(angle) > 1e-6:
        for exact in (0.0, 0.5, -0.5, 1.0, -1.0):
            if abs(value - exact) < 1e-14:
                return exact
    return value


def _trig(function):
    return lambda x: _snap(function(float(x)), float(x))


def _degrees(function):
    return lambda x: _snap(function(math.radians(float(x))), float(x))


def _tan(radians):
    result = math.tan(radians)
    if abs(result) > 1e15:
        raise ValueError("tangent is undefined at this angle")
    return result


def _sqrt(x):
    if isinstance(x, Fraction) and x >= 0:
        top, bottom = math.isqrt(x.numerator), math.isqrt(x.denominator)
        if top * top == x.numerator and bottom * bottom == x.denominator:
            return Fraction(top, bottom)
    return math.sqrt(float(x))


def _cbrt(x):
    root = abs(float(x)) ** (1 / 3)
    root = math.copysign(root, float(x))
    if isinstance(x, Fraction) and x.denominator == 1 and round(root) ** 3 == x.numerator:
        return Fraction(round(root))
    return root


def _log(x, base=None):
    return math.log(float(x)) if base is None else math.log(float(x), float(base))


def _round(x, places=Fraction(0)):
    """Rounds halves away from zero, like a scientific calculator."""
    places = _whole(places)
    if abs(places) > 100:
        raise OverflowError
    exact = Fraction(str(x)) if isinstance(x, float) else x
    scale = Fraction(10) ** places
    rounded = math.floor(abs(exact) * scale + Fraction(1, 2)) / scale
    return Fraction(rounded) if exact >= 0 else -Fraction(rounded)


def _power(base, exponent):
    if isinstance(base, Fraction) and isinstance(exponent, Fraction) and exponent.denominator == 1:
        size = max(base.numerator.bit_length(), base.denominator.bit_length())
        if size * abs(exponent.numerator) > MAX_BITS:
            raise OverflowError
        return base ** exponent.numerator
    result = float(base) ** float(exponent)
    if isinstance(result, complex):
        raise ValueError("the result isn't a real number")
    return result


def _median(*values):
    ordered, middle = sorted(values), len(values) // 2
    return ordered[middle] if len(values) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _floats(function):
    return lambda *values: function([float(v) for v in values])


CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
FUNCTIONS = {
    "sin": _trig(math.sin), "cos": _trig(math.cos), "tan": _trig(_tan),
    "sind": _degrees(math.sin), "cosd": _degrees(math.cos), "tand": _degrees(_tan),
    "asin": lambda x: math.asin(float(x)), "acos": lambda x: math.acos(float(x)),
    "atan": lambda x: math.atan(float(x)), "atan2": lambda y, x: math.atan2(float(y), float(x)),
    "sinh": lambda x: math.sinh(float(x)), "cosh": lambda x: math.cosh(float(x)),
    "tanh": lambda x: math.tanh(float(x)), "asinh": lambda x: math.asinh(float(x)),
    "acosh": lambda x: math.acosh(float(x)), "atanh": lambda x: math.atanh(float(x)),
    "degrees": lambda x: math.degrees(float(x)), "radians": lambda x: math.radians(float(x)),
    "sqrt": _sqrt, "cbrt": _cbrt, "exp": lambda x: math.exp(float(x)), "pow": _power,
    "log": _log, "ln": lambda x: math.log(float(x)),
    "log10": lambda x: math.log10(float(x)), "log2": lambda x: math.log2(float(x)),
    "abs": abs, "round": _round, "floor": lambda x: Fraction(math.floor(x)), "ceil": lambda x: Fraction(math.ceil(x)),
    "hypot": lambda *v: math.hypot(*[float(x) for x in v]),
    "factorial": lambda n: Fraction(math.factorial(_counting(n))),
    "comb": lambda n, k: Fraction(math.comb(_counting(n), _counting(k))),
    "perm": lambda n, k: Fraction(math.perm(_counting(n), _counting(k))),
    "gcd": lambda *v: Fraction(math.gcd(*[_whole(x) for x in v])),
    "lcm": lambda *v: Fraction(math.lcm(*[_whole(x) for x in v])),
    "min": lambda *v: min(v), "max": lambda *v: max(v), "sum": lambda *v: sum(v),
    "mean": lambda *v: sum(v) / len(v), "median": _median,
    "stdev": _floats(statistics.stdev), "pstdev": _floats(statistics.pstdev),
    "variance": _floats(statistics.variance),
}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: _power}


def _checked(value):
    if isinstance(value, Fraction) and max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise OverflowError
    return value


def _evaluate(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise OverflowError
        return Fraction(node.value) if isinstance(node.value, int) else Fraction(str(node.value))
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _checked(_BINARY[type(node.op)](_evaluate(node.left), _evaluate(node.right)))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
        return _checked(FUNCTIONS[node.func.id](*[_evaluate(a) for a in node.args]))
    raise ValueError("unsupported expression")


def _format(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OverflowError
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_SHOWN_BITS:
        with localcontext() as context:
            context.prec = 16
            context.Emax = 999999
            approximate = Decimal(value.numerator) / Decimal(value.denominator)
            return f"approximately {approximate:E}"
    if value.denominator == 1:
        return str(value.numerator)
    with localcontext() as context:
        context.prec = 60
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
        if Fraction(decimal) == value:
            return format(decimal.normalize(), "f")
        context.prec = 16
        return f"{+decimal:g} (exactly {value.numerator}/{value.denominator})"


def calculator(expression):
    """Evaluate a math expression and return the result."""
    text = str(expression).strip().replace("^", "**")
    if not text:
        return "Error: enter an expression."
    if len(text) > MAX_EXPRESSION:
        return f"Error: expressions are limited to {MAX_EXPRESSION} characters."
    try:
        return f"{expression} = {_format(_evaluate(ast.parse(text, mode='eval').body))}"
    except ZeroDivisionError:
        return "Error: division by zero."
    except SyntaxError:
        return "Error: that isn't a valid expression. Example: sqrt(2) * (3 + 4)^2"
    except (OverflowError, MemoryError, RecursionError):
        return "Error: the result is too large to calculate."
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}."
