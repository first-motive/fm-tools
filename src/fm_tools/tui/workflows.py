"""Typed workflow controls. Execution always uses the existing FM dispatcher.

A field contains one value, never an argument string. Repository workflows can
provide the same explicit contract in their manifest's ``tui`` section.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    flag: str = ""
    kind: str = "text"
    choices: tuple[str, ...] = ()
    default: str = ""
    required: bool = False
    help: str = ""
    multiple: bool = False
    repeat: bool = False
    arity: int = 0
    exclusive: str = ""
    group_required: bool = False


@dataclass(frozen=True)
class Action:
    key: str
    title: str
    argv: tuple[str, ...]
    fields: tuple[Field, ...] = ()
    description: str = ""
    group: str = "workspace"
    report: bool = False
    effects: str = ""
    request_operation: str = ""
    request_body: str = ""


def label(value: str) -> str:
    names = {
        "dir": "folder",
        "out": "output",
        "fps": "frames per second",
        "num": "number",
        "max": "maximum",
        "min": "minimum",
        "cfg": "configuration",
    }
    return " ".join(
        names.get(word, word)
        for word in value.replace("_", " ").replace("-", " ").split()
    ).capitalize()


def fields_from_parser(parser: argparse.ArgumentParser) -> tuple[Field, ...]:
    fields = []
    for arg in parser._actions:
        if isinstance(
            arg, (argparse._HelpAction, argparse._SubParsersAction)
        ) or arg.dest in ("json", "as_json"):
            continue
        flag = next(
            (v for v in arg.option_strings if v.startswith("--")),
            next(iter(arg.option_strings), ""),
        )
        boolean = isinstance(
            arg, (argparse._StoreTrueAction, argparse._StoreFalseAction)
        )
        kind = (
            "boolean"
            if boolean
            else "number"
            if arg.type in (int, float)
            else "path"
            if arg.type is Path
            else "text"
        )
        choices = tuple(str(v) for v in arg.choices) if arg.choices else ()
        # Leave optional defaults to the owner. Sending a displayed default can
        # change meaning when an installer distinguishes absent and explicit input.
        default = ""
        exclusive = next(
            (g for g in parser._mutually_exclusive_groups if arg in g._group_actions),
            None,
        )
        fields.append(
            Field(
                arg.dest,
                label(arg.dest),
                flag,
                kind,
                choices,
                default,
                bool(arg.required or (not flag and arg.nargs not in ("?", "*"))),
                "" if arg.help == argparse.SUPPRESS else str(arg.help or ""),
                isinstance(arg, argparse._AppendAction)
                or arg.nargs in ("+", "*")
                or isinstance(arg.nargs, int),
                isinstance(arg, argparse._AppendAction),
                arg.nargs if isinstance(arg.nargs, int) else 0,
                ":".join(a.dest for a in exclusive._group_actions) if exclusive else "",
                bool(exclusive and exclusive.required),
            )
        )
    return tuple(fields)


def parser_actions(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...],
    group: str,
    inherited: tuple[Field, ...] = (),
) -> list[Action]:
    fields = inherited + fields_from_parser(parser)
    sub = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
    )
    if sub:
        actions = []
        for name, child in sub.choices.items():
            actions.extend(parser_actions(child, (*prefix, name), group, fields))
        return actions
    return [
        Action(
            "/".join(prefix),
            " / ".join(label(v) for v in prefix[1:]) or label(prefix[0]),
            prefix,
            fields,
            parser.description or "",
            group,
        )
    ]


def value_arguments(action: Action, values: dict[str, str | bool]) -> tuple[str, ...]:
    groups = {field.exclusive for field in action.fields if field.exclusive}
    for group in groups:
        fields = [field for field in action.fields if field.exclusive == group]
        count = sum(bool(values.get(field.key, "")) for field in fields)
        if count > 1 or (count == 0 and any(field.group_required for field in fields)):
            raise ValueError(
                "Choose one: " + " or ".join(field.label for field in fields) + "."
            )
    argv = [
        str(values.get(part[1:-1], ""))
        if part.startswith("{") and part.endswith("}")
        else part
        for part in action.argv
    ]
    for field in action.fields:
        value = values.get(field.key, "")
        if field.kind == "boolean":
            if value:
                argv.append(field.flag)
            continue
        value = str(value).strip()
        if not value:
            if field.required:
                raise ValueError(f"{field.label}: choose or enter a value.")
            continue
        if any(not char.isprintable() and char != "\n" for char in value):
            raise ValueError(f"{field.label}: control characters are not allowed.")
        items = (
            [v.strip() for v in value.splitlines() if v.strip()]
            if field.multiple
            else [value]
        )
        if field.arity and len(items) != field.arity:
            raise ValueError(
                f"{field.label}: enter {field.arity} values, one per line."
            )
        for index, item in enumerate(items):
            if field.choices and item not in field.choices:
                raise ValueError(f"{field.label}: choose one of the available values.")
            if field.kind == "number":
                try:
                    float(item)
                except ValueError:
                    raise ValueError(f"{field.label}: enter a number.") from None
            if field.flag.startswith("$"):
                continue
            if item.startswith("-") and field.kind != "number":
                raise ValueError(f"{field.label}: a value must not begin with a dash.")
            if "{" + field.key + "}" in action.argv:
                continue
            if field.flag:
                if field.repeat or index == 0:
                    argv.append(field.flag)
                argv.append(item)
            else:
                if item.startswith("-"):
                    raise ValueError(
                        f"{field.label}: the value must not start with a dash."
                    )
                argv.append(item)
    return tuple(argv)
