"""V2.2 skill run plan: skill steps become ordinary chain proposals, no grant.

A skill is a procedure, never an authority. ``plan_steps`` turns the steps of
one saved ``Skill`` into a tuple of ``contracts.ChainStep`` proposals, the
same inert declaration a model-proposed chain is made of. Core then treats
them exactly like any other declaration: the one chain grammar judges them,
``AgentCore.build_chain_step_task`` turns each into an ordinary ``Task`` with
no grant, and every step meets Core policy and approval by itself.

What a proposal can carry is fixed by ``ChainStep``: objective, capability,
action, target, destination, write mode and predecessor. There is no field
for a permission grant, an approval flag, a risk level or a remembered scope,
and this planner fills only four of the seven (decision D12, the step
contract shared with SKDRAFT):

- ``objective``: the step's action text with each placeholder replaced by its
  checked input value. A placeholder is exactly ``{name}`` with ``name`` in
  ``step.inputs``; every brace in the action must belong to one. The
  objective is ONE line: the action and every value are refused if they hold
  a tab, newline, carriage return or any other character refused below.
  Bounded by the chain objective ceiling; an objective that would exceed it
  is refused, never truncated.
- ``capability``: the step's hint, verbatim. A hint is not a decision. The
  planner never invents one (a step without a hint is refused) and never
  checks one against a registry: an unknown capability is left for Core to
  refuse.
- ``action``: the action symbol Core's trusted catalog
  (``SkillPlanContext.actions``) names for that capability. A capability the
  catalog does not name gets the EMPTY action, which the one chain grammar
  refuses, so Core refuses the whole chain before any task exists.
- ``target``: the value of the step's FIRST input (``step.inputs[0]``, the
  primary object the step acts on), or empty when the step has no inputs.
  Core's own grammar and the connector still judge it.

``destination`` and ``write_mode`` are always empty and ``consumes`` is
always ``CHAIN_NO_PREDECESSOR``: where data may go, whether bytes may be
written and which result feeds which step are Core decisions, never skill
content. A skill that would need them is refused by Core, not widened here.

Refusals (``SkillPlanError``, a ``ValueError``) are raised before anything is
returned: a record that is not an exact ``Skill``, a project skill outside
its project, more steps than one chain holds, a missing capability hint, a
malformed placeholder or a placeholder naming an input the step does not
list, an action that is not one line, an undeclared or malformed input value,
a step whose primary or placeholder input has no value, an over-long
objective, an inputs mapping that cannot be read, and missing required inputs
(``MissingInputsError`` lists every one; nothing is guessed).

Skill health and use counts are not inputs: nothing here can see them, so a
skill used a thousand times plans exactly like a brand-new one.

Pure: no I/O, no clock, no randomness; same input, same output.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from jarvis.contracts import (
    CHAIN_LOCATOR_MAX_CHARS,
    CHAIN_NO_PREDECESSOR,
    CHAIN_OBJECTIVE_MAX_CHARS,
    MAX_CHAIN_STEPS,
    ChainStep,
    chain_locator_refusal,
    chain_objective_refusal,
    chain_symbol_refusal,
)
from jarvis.skill_types import ID_PATTERN, Skill, SkillScopeKind

#: Hard ceiling on one supplied input value.
INPUT_VALUE_MAX = CHAIN_LOCATOR_MAX_CHARS

#: Hard ceiling on the trusted capability-to-action catalog.
MAX_CATALOG_ENTRIES = 256

_OPEN = "{"
_CLOSE = "}"

#: Unicode categories refused in a step action and in an input value: the
#: categories skill_types refuses in text (controls, surrogates, format
#: characters such as zero-width, BOM, soft hyphen, TAG and bidi characters,
#: private use, unassigned code points, line and paragraph separators). Here
#: tab, newline and carriage return (all "Cc") are refused too, so the
#: objective stays one line and nothing can add a line to it.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs", "Cf", "Co", "Cn", "Zl", "Zp"})

#: Letters that render blank (Hangul fillers, halfwidth Hangul filler) and the
#: blank Braille pattern: refused in an input value, so a value shown on an
#: approval card cannot look like a different value.
_BLANK_LOOKING = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800)))

#: D17, mirrored from skill_types (which keeps it private): every
#: Default_Ignorable_Code_Point (Unicode DerivedCoreProperties) as an explicit,
#: version-independent list, plus U+2800. Several are category Mn or Lo
#: (CGJ, Khmer inherent vowels, Mongolian FVS, variation selectors, Hangul
#: fillers), so the category check alone would let them hide a payload in a
#: value; skill_types refuses every one of them in text, and so does this rule.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
    (0x2800, 0x2800),
)


class SkillPlanError(ValueError):
    """A skill cannot be planned as given; nothing was proposed."""


class MissingInputsError(SkillPlanError):
    """Required skill inputs were not supplied; ``missing`` names every one."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("missing required skill inputs: " + ", ".join(missing))


@dataclass(frozen=True, slots=True)
class SkillPlanContext:
    """What the trusted caller knows about the task the skill would serve.

    ``project_id``: the active project, or ``None`` outside a project. A
    project skill is planned only inside its own project.

    ``actions``: Core's trusted catalog, capability name -> action symbol (for
    example the action each natural-language capability already uses). The
    planner only looks names up in it; it never adds to it.

    Deliberately nothing else: no health, no use count, no approval, grant,
    trust, risk or permission state can be handed to the planner.
    """

    project_id: str | None = None
    actions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.project_id is not None and (
            type(self.project_id) is not str
            or ID_PATTERN.fullmatch(self.project_id) is None
        ):
            raise SkillPlanError("the context project id is malformed")
        try:
            if not isinstance(self.actions, Mapping):
                raise SkillPlanError("the context action catalog is not a mapping")
            snapshot = dict(self.actions)
        except SkillPlanError:
            raise
        except Exception as exc:
            raise SkillPlanError("the context action catalog cannot be read") from exc
        if len(snapshot) > MAX_CATALOG_ENTRIES:
            raise SkillPlanError("the context action catalog is too large")
        for capability, action in snapshot.items():
            if (
                chain_symbol_refusal(capability) is not None
                or chain_symbol_refusal(action) is not None
            ):
                raise SkillPlanError("the context action catalog has a malformed entry")
        object.__setattr__(self, "actions", MappingProxyType(snapshot))


def _is_invisible(character: str) -> bool:
    code_point = ord(character)
    return any(low <= code_point <= high for low, high in _INVISIBLE_RANGES)


def _has_refused_character(text: str) -> bool:
    return any(
        unicodedata.category(character) in _REFUSED_CATEGORIES or _is_invisible(character)
        for character in text
    )


def _value_refusal(value: Any) -> str | None:
    if type(value) is not str:
        return "is not a string"
    if len(value) > INPUT_VALUE_MAX:
        return "is longer than the input ceiling"
    if value != value.strip():
        return "carries surrounding whitespace"
    if any(character in _BLANK_LOOKING for character in value):
        return "contains a blank-looking character"
    if _has_refused_character(value):
        return "contains a control, line-break, invisible, private-use or unassigned character"
    return None


def _supplied_values(skill: Skill, inputs: Any) -> dict[str, str]:
    """The supplied values, copied once and checked once. Blank means not supplied."""
    try:
        if not isinstance(inputs, Mapping):
            raise SkillPlanError("inputs must be a mapping of input name to value")
        snapshot = dict(inputs)
    except SkillPlanError:
        raise
    except Exception as exc:
        raise SkillPlanError("the inputs mapping cannot be read") from exc
    declared = {item.name for item in skill.inputs}
    values: dict[str, str] = {}
    for name, value in snapshot.items():
        if type(name) is not str:
            raise SkillPlanError("input names must be strings")
        if name not in declared:
            raise SkillPlanError("an input the skill does not declare was supplied")
        if type(value) is str and not value.strip():
            continue
        reason = _value_refusal(value)
        if reason is not None:
            raise SkillPlanError(f"input {name} {reason}")
        values[name] = value
    missing = tuple(
        item.name for item in skill.inputs if item.required and item.name not in values
    )
    if missing:
        raise MissingInputsError(missing)
    return values


def _value_of(name: str, values: dict[str, str], index: int) -> str:
    if name not in values:
        raise SkillPlanError(f"step {index} uses input {name}, which has no value")
    return values[name]


def _objective(action: str, names: tuple[str, ...], values: dict[str, str], index: int) -> str:
    """The action with each ``{name}`` placeholder replaced; one line, or refused."""
    if _has_refused_character(action):
        raise SkillPlanError(f"step {index} action is not one line of visible text")
    stray = f"step {index} action has a brace outside a placeholder"
    parts: list[str] = []
    position = 0
    while True:
        opening = action.find(_OPEN, position)
        if action.find(_CLOSE, position, len(action) if opening == -1 else opening) != -1:
            raise SkillPlanError(stray)
        if opening == -1:
            parts.append(action[position:])
            return "".join(parts)
        closing = action.find(_CLOSE, opening + 1)
        if closing == -1:
            raise SkillPlanError(stray)
        name = action[opening + 1 : closing]
        if _OPEN in name:
            raise SkillPlanError(stray)
        if name not in names:
            raise SkillPlanError(
                f"step {index} action has a placeholder for an input the step does not list"
            )
        parts.append(action[position:opening])
        parts.append(_value_of(name, values, index))
        position = closing + 1


def plan_steps(
    skill: Skill, inputs: Mapping[str, str], task_context: SkillPlanContext
) -> tuple[ChainStep, ...]:
    """The skill's steps as ordinary chain proposals; raises ``SkillPlanError``.

    The result is inert data with no authority. Feed it to Core the way a
    model-proposed chain arrives (``[step.to_dict() for step in result]``
    through ``chain_steps_from_declaration``, then ``ChainState.declared``):
    the one chain grammar judges it and every step still meets Core policy and
    approval as its own ordinary task.
    """
    if type(skill) is not Skill:
        raise SkillPlanError("only a saved Skill can be planned")
    if type(task_context) is not SkillPlanContext:
        raise SkillPlanError("the task context is not a SkillPlanContext")
    if (
        skill.scope.kind is SkillScopeKind.PROJECT
        and skill.scope.project_id != task_context.project_id
    ):
        raise SkillPlanError("a project skill is planned only inside its own project")
    total = len(skill.steps)
    if total > MAX_CHAIN_STEPS:
        raise SkillPlanError(
            f"the skill has {total} steps; one chain holds at most {MAX_CHAIN_STEPS}"
        )
    values = _supplied_values(skill, inputs)
    proposals: list[ChainStep] = []
    for step in skill.steps:
        if step.capability is None:
            raise SkillPlanError(
                f"step {step.index} names no capability; a skill never invents one"
            )
        objective = _objective(step.action, step.inputs, values, step.index)
        if chain_objective_refusal(objective) is not None:
            raise SkillPlanError(
                f"step {step.index} does not fit one chain objective "
                f"({CHAIN_OBJECTIVE_MAX_CHARS} characters at most)"
            )
        target = _value_of(step.inputs[0], values, step.index) if step.inputs else ""
        if chain_locator_refusal(target) is not None:
            raise SkillPlanError(f"step {step.index} target is malformed")
        proposals.append(
            ChainStep(
                objective=objective,
                capability=step.capability,
                action=task_context.actions.get(step.capability, ""),
                target=target,
                destination="",
                write_mode="",
                consumes=CHAIN_NO_PREDECESSOR,
            )
        )
    return tuple(proposals)
