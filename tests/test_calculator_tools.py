"""Tests for the calculator tool."""

import math
from collections.abc import Awaitable, Callable

import pytest

from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.calculator_tools import _create_calculator_tools


def _get_calculate() -> Callable[..., Awaitable[ToolResult]]:
    """Get the calculate tool function."""
    tools = _create_calculator_tools()
    return tools[0].function


# --- Basic arithmetic ---


@pytest.mark.asyncio()
async def test_addition() -> None:
    calc = _get_calculate()
    result = await calc(expression="2 + 3")
    assert result.content == "5"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_subtraction() -> None:
    calc = _get_calculate()
    result = await calc(expression="10 - 4")
    assert result.content == "6"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_multiplication() -> None:
    calc = _get_calculate()
    result = await calc(expression="6 * 7")
    assert result.content == "42"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_division() -> None:
    calc = _get_calculate()
    result = await calc(expression="15 / 3")
    assert result.content == "5"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_floor_division() -> None:
    calc = _get_calculate()
    result = await calc(expression="7 // 2")
    assert result.content == "3"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_modulo() -> None:
    calc = _get_calculate()
    result = await calc(expression="7 % 2")
    assert result.content == "1"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_exponentiation() -> None:
    calc = _get_calculate()
    result = await calc(expression="2 ** 10")
    assert result.content == "1024"
    assert result.is_error is False


# --- Order of operations and parentheses ---


@pytest.mark.asyncio()
async def test_order_of_operations() -> None:
    calc = _get_calculate()
    result = await calc(expression="2 + 3 * 4")
    assert result.content == "14"


@pytest.mark.asyncio()
async def test_parentheses() -> None:
    calc = _get_calculate()
    result = await calc(expression="(2 + 3) * 4")
    assert result.content == "20"


@pytest.mark.asyncio()
async def test_nested_parentheses() -> None:
    calc = _get_calculate()
    result = await calc(expression="((2 + 3) * (4 - 1))")
    assert result.content == "15"


# --- Floating point ---


@pytest.mark.asyncio()
async def test_floating_point() -> None:
    calc = _get_calculate()
    result = await calc(expression="3.14 * 2")
    assert result.content == "6.28"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_float_formatting_no_noise() -> None:
    """0.1 + 0.2 should not show 0.30000000000000004."""
    calc = _get_calculate()
    result = await calc(expression="0.1 + 0.2")
    assert result.content == "0.3"


# --- Unary operators ---


@pytest.mark.asyncio()
async def test_unary_negation() -> None:
    calc = _get_calculate()
    result = await calc(expression="-5 + 3")
    assert result.content == "-2"


# --- Math functions ---


@pytest.mark.asyncio()
async def test_sqrt() -> None:
    calc = _get_calculate()
    result = await calc(expression="sqrt(16)")
    assert result.content == "4"
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_abs() -> None:
    calc = _get_calculate()
    result = await calc(expression="abs(-5)")
    assert result.content == "5"


@pytest.mark.asyncio()
async def test_round() -> None:
    calc = _get_calculate()
    result = await calc(expression="round(3.14159, 2)")
    assert result.content == "3.14"


@pytest.mark.asyncio()
async def test_ceil() -> None:
    calc = _get_calculate()
    result = await calc(expression="ceil(3.2)")
    assert result.content == "4"


@pytest.mark.asyncio()
async def test_floor() -> None:
    calc = _get_calculate()
    result = await calc(expression="floor(3.8)")
    assert result.content == "3"


@pytest.mark.asyncio()
async def test_min() -> None:
    calc = _get_calculate()
    result = await calc(expression="min(3, 5, 1)")
    assert result.content == "1"


@pytest.mark.asyncio()
async def test_max() -> None:
    calc = _get_calculate()
    result = await calc(expression="max(3, 5, 1)")
    assert result.content == "5"


# --- Constants ---


@pytest.mark.asyncio()
async def test_pi() -> None:
    calc = _get_calculate()
    result = await calc(expression="pi")
    assert float(result.content) == pytest.approx(math.pi)


@pytest.mark.asyncio()
async def test_e() -> None:
    calc = _get_calculate()
    result = await calc(expression="e")
    assert float(result.content) == pytest.approx(math.e)


@pytest.mark.asyncio()
async def test_pi_in_expression() -> None:
    calc = _get_calculate()
    result = await calc(expression="pi * 2")
    assert float(result.content) == pytest.approx(math.pi * 2)


# --- Contractor math examples ---


@pytest.mark.asyncio()
async def test_square_footage() -> None:
    calc = _get_calculate()
    result = await calc(expression="12 * 15")
    assert result.content == "180"


@pytest.mark.asyncio()
async def test_markup() -> None:
    calc = _get_calculate()
    result = await calc(expression="round(2450 * 1.25, 2)")
    assert result.content == "3062.5"


@pytest.mark.asyncio()
async def test_material_cost_with_waste() -> None:
    calc = _get_calculate()
    result = await calc(expression="round(150 * 1.10 * 3.50, 2)")
    assert result.content == "577.5"


# --- Error cases ---


@pytest.mark.asyncio()
async def test_division_by_zero() -> None:
    calc = _get_calculate()
    result = await calc(expression="1 / 0")
    assert result.is_error is True
    assert "division by zero" in result.content


@pytest.mark.asyncio()
async def test_invalid_syntax() -> None:
    calc = _get_calculate()
    result = await calc(expression="2 +")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_empty_expression() -> None:
    calc = _get_calculate()
    result = await calc(expression="")
    assert result.is_error is True
    assert "empty" in result.content


@pytest.mark.asyncio()
async def test_whitespace_only() -> None:
    calc = _get_calculate()
    result = await calc(expression="   ")
    assert result.is_error is True
    assert "empty" in result.content


@pytest.mark.asyncio()
async def test_unknown_variable() -> None:
    calc = _get_calculate()
    result = await calc(expression="foo + 1")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_unknown_function() -> None:
    calc = _get_calculate()
    result = await calc(expression="sin(1)")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_huge_exponent_rejected() -> None:
    calc = _get_calculate()
    result = await calc(expression="2 ** 5000000")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_infinity_result() -> None:
    calc = _get_calculate()
    result = await calc(expression="1e308 * 2")
    assert result.is_error is True
    assert "not a finite number" in result.content


# --- Security ---


@pytest.mark.asyncio()
async def test_import_rejected() -> None:
    calc = _get_calculate()
    result = await calc(expression='__import__("os")')
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_attribute_access_rejected() -> None:
    calc = _get_calculate()
    result = await calc(expression="().__class__")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_eval_rejected() -> None:
    calc = _get_calculate()
    result = await calc(expression='eval("1+1")')
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_open_rejected() -> None:
    calc = _get_calculate()
    result = await calc(expression='open("/etc/passwd")')
    assert result.is_error is True


# --- Large numbers ---


@pytest.mark.asyncio()
async def test_large_multiplication() -> None:
    calc = _get_calculate()
    result = await calc(expression="999999999 * 999999999")
    assert result.content == "999999998000000001"
    assert result.is_error is False


# --- Tool metadata ---


def test_tool_name() -> None:
    tools = _create_calculator_tools()
    assert tools[0].name == "calculate"


def test_tool_has_description() -> None:
    tools = _create_calculator_tools()
    assert "arithmetic" in tools[0].description.lower() or "math" in tools[0].description.lower()


def test_tool_has_usage_hint() -> None:
    tools = _create_calculator_tools()
    assert tools[0].usage_hint
