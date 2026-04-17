"""Calculator tool for the agent.

Uses simpleeval for safe mathematical expression evaluation.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from simpleeval import (
    FeatureNotAvailable,
    FunctionNotDefined,
    NameNotDefined,
    NumberTooHigh,
    simple_eval,
)

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext


CALC_FUNCTIONS: dict[str, object] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "min": min,
    "max": max,
}

CALC_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
}


class CalculateParams(BaseModel):
    """Parameters for the calculate tool."""

    expression: str = Field(
        max_length=1000,
        description="A mathematical expression to evaluate, e.g. '(12 * 15) + (8 * 10)'",
    )


def _create_calculator_tools() -> list[Tool]:
    """Create the calculator tool for the agent."""

    async def calculate(expression: str) -> ToolResult:
        """Evaluate a mathematical expression and return the result."""
        if not expression.strip():
            return ToolResult(
                content="Error: empty expression",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Provide a mathematical expression to evaluate.",
            )

        try:
            result = simple_eval(expression, functions=CALC_FUNCTIONS, names=CALC_NAMES)
        except ZeroDivisionError:
            return ToolResult(
                content="Error: division by zero",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Check the expression for division by zero.",
            )
        except SyntaxError:
            return ToolResult(
                content=f"Error: invalid syntax in expression: {expression}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Check the expression for syntax errors.",
            )
        except (NameNotDefined, FunctionNotDefined, FeatureNotAvailable) as exc:
            return ToolResult(
                content=f"Error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint=(
                    "Only basic arithmetic, parentheses, and these functions are supported: "
                    "sqrt, abs, round, ceil, floor, min, max. "
                    "Constants: pi, e."
                ),
            )
        except NumberTooHigh as exc:
            return ToolResult(
                content=f"Error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Use smaller exponents. The power operator is capped for safety.",
            )
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResult(
                content=f"Error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
                hint="Check the expression for type or value errors.",
            )

        if isinstance(result, float):
            if math.isinf(result) or math.isnan(result):
                return ToolResult(
                    content="Error: result is not a finite number",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                    hint="The expression produced infinity or NaN. Check for overflow.",
                )
            formatted = f"{result:.10g}"
        else:
            formatted = str(result)

        return ToolResult(content=formatted)

    return [
        Tool(
            name=ToolName.CALCULATE,
            description=(
                "Evaluate a mathematical expression and return the exact result. "
                "Use this tool for ALL arithmetic instead of computing in your head. "
                "Examples: '12.5 * 47 * 1.15' for material cost with markup, "
                "'(24 * 36) / 144' for square footage to square yards, "
                "'round(2450 * 1.25, 2)' for 25% markup, "
                "'ceil(sqrt(400))' for square root rounded up."
            ),
            function=calculate,
            params_model=CalculateParams,
            usage_hint=(
                "Use this tool whenever the user asks you to do math, calculate costs, "
                "estimate quantities, compute areas, or any arithmetic. Always prefer "
                "this tool over doing math in your head. Supported functions: "
                "sqrt, abs, round, ceil, floor, min, max. Constants: pi, e."
            ),
        ),
    ]


def _calculator_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for calculator tools, used by the registry."""
    return _create_calculator_tools()


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "calculator",
        _calculator_factory,
        core=True,
        summary="Evaluate mathematical expressions",
        sub_tools=[
            SubToolInfo(ToolName.CALCULATE, "Evaluate a math expression"),
        ],
    )


_register()
