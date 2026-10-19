"""Translate an Expression to a Type value."""

from __future__ import annotations

from collections.abc import Callable

from mypy.fastparse import parse_type_string
from mypy.nodes import (
    MISSING_FALLBACK,
    BytesExpr,
    CallExpr,
    ComplexExpr,
    ConditionalExpr,
    Context,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    Expression,
    FloatExpr,
    IndexExpr,
    IntExpr,
    ListComprehension,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    RefExpr,
    StarExpr,
    StrExpr,
    SymbolTableNode,
    TupleExpr,
    UnaryExpr,
    get_member_expr_fullname,
)
from mypy.options import Options
from mypy.types import (
    ANNOTATED_TYPE_NAMES,
    AnyType,
    CallableArgument,
    EllipsisType,
    Instance,
    ProperType,
    RawExpressionType,
    Type,
    TypedDictType,
    TypeForComprehension,
    TypeList,
    TypeOfAny,
    UnboundType,
    UnionType,
    UnpackType,
)


class TypeTranslationError(Exception):
    """Exception raised when an expression is not valid as a type."""


def _extract_argument_name(expr: Expression) -> str | None:
    if isinstance(expr, NameExpr) and expr.name == "None":
        return None
    elif isinstance(expr, StrExpr):
        return expr.value
    else:
        raise TypeTranslationError()


def expr_to_unanalyzed_type(
    expr: Expression,
    options: Options,
    allow_new_syntax: bool = False,
    _parent: Expression | None = None,
    allow_unpack: bool = False,
    lookup_qualified: Callable[[str, Context], SymbolTableNode | None] | None = None,
) -> Type:
    """Translate an expression to the corresponding type.

    The result is not semantically analyzed. It can be UnboundType or TypeList.
    Raise TypeTranslationError if the expression cannot represent a type.

    If lookup_qualified is not provided, the expression is expected to be semantically
    analyzed.

    If allow_new_syntax is True, allow all type syntax independent of the target
    Python version (used in stubs).

    # TODO: a lot of code here is duplicated in fastparse.py, refactor this.
    """
    # The `parent` parameter is used in recursive calls to provide context for
    # understanding whether an CallableArgument is ok.
    name: str | None = None
    if isinstance(expr, NameExpr):
        name = expr.name
        if name == "True":
            return RawExpressionType(True, "builtins.bool", line=expr.line, column=expr.column)
        elif name == "False":
            return RawExpressionType(False, "builtins.bool", line=expr.line, column=expr.column)
        else:
            return UnboundType(name, line=expr.line, column=expr.column)
    elif isinstance(expr, MemberExpr):
        fullname = get_member_expr_fullname(expr)
        if fullname:
            return UnboundType(fullname, line=expr.line, column=expr.column)
        else:
            # Attribute access on a complex type expression (subscripted, conditional, etc.)
            # Desugar X.attr to _TypeGetAttr[X, Literal["attr"]]
            before_dot = expr_to_unanalyzed_type(
                expr.expr, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
            )
            attr_literal = RawExpressionType(expr.name, "builtins.str", line=expr.line)
            return UnboundType(
                "__builtins__._TypeGetAttr",
                [before_dot, attr_literal],
                line=expr.line,
                column=expr.column,
            )
    elif isinstance(expr, IndexExpr):
        base = expr_to_unanalyzed_type(
            expr.base, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
        )
        if isinstance(base, UnboundType):
            if base.args:
                raise TypeTranslationError()
            if isinstance(expr.index, TupleExpr):
                args = expr.index.items
            else:
                args = [expr.index]

            if isinstance(expr.base, RefExpr):
                # Check if the type is Annotated[...]. For this we need the fullname,
                # which must be looked up if the expression hasn't been semantically analyzed.
                base_fullname = None
                if lookup_qualified is not None:
                    sym = lookup_qualified(base.name, expr)
                    if sym and sym.node:
                        base_fullname = sym.node.fullname
                else:
                    base_fullname = expr.base.fullname

                if base_fullname is not None and base_fullname in ANNOTATED_TYPE_NAMES:
                    # TODO: this is not the optimal solution as we are basically getting rid
                    # of the Annotation definition and only returning the type information,
                    # losing all the annotations.
                    return expr_to_unanalyzed_type(
                        args[0], options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
                    )
            base.args = tuple(
                expr_to_unanalyzed_type(
                    arg,
                    options,
                    allow_new_syntax,
                    expr,
                    allow_unpack=True,
                    lookup_qualified=lookup_qualified,
                )
                for arg in args
            )
            if not base.args:
                base.empty_tuple_index = True
            return base
        else:
            raise TypeTranslationError()
    elif (
        isinstance(expr, OpExpr)
        and expr.op == "|"
        and ((options.python_version >= (3, 10)) or allow_new_syntax)
    ):
        return UnionType(
            [
                expr_to_unanalyzed_type(
                    expr.left, options, allow_new_syntax, lookup_qualified=lookup_qualified
                ),
                expr_to_unanalyzed_type(
                    expr.right, options, allow_new_syntax, lookup_qualified=lookup_qualified
                ),
            ],
            uses_pep604_syntax=True,
        )
    elif isinstance(expr, OpExpr) and expr.op in ("and", "or"):
        # Convert `A and B` to `_And[A, B]` and `A or B` to `_Or[A, B]`
        op_name = "_And" if expr.op == "and" else "_Or"
        return UnboundType(
            f"__builtins__.{op_name}",
            [
                expr_to_unanalyzed_type(
                    expr.left, options, allow_new_syntax, lookup_qualified=lookup_qualified
                ),
                expr_to_unanalyzed_type(
                    expr.right, options, allow_new_syntax, lookup_qualified=lookup_qualified
                ),
            ],
            line=expr.line,
            column=expr.column,
        )
    elif isinstance(expr, CallExpr) and isinstance(_parent, ListExpr):
        c = expr.callee
        names = []
        # Go through the dotted member expr chain to get the full arg
        # constructor name to look up
        while True:
            if isinstance(c, NameExpr):
                names.append(c.name)
                break
            elif isinstance(c, MemberExpr):
                names.append(c.name)
                c = c.expr
            else:
                raise TypeTranslationError()
        arg_const = ".".join(reversed(names))

        # Go through the constructor args to get its name and type.
        name = None
        default_type = AnyType(TypeOfAny.unannotated)
        typ: Type = default_type
        for i, arg in enumerate(expr.args):
            if expr.arg_names[i] is not None:
                if expr.arg_names[i] == "name":
                    if name is not None:
                        # Two names
                        raise TypeTranslationError()
                    name = _extract_argument_name(arg)
                    continue
                elif expr.arg_names[i] == "type":
                    if typ is not default_type:
                        # Two types
                        raise TypeTranslationError()
                    typ = expr_to_unanalyzed_type(
                        arg, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
                    )
                    continue
                else:
                    raise TypeTranslationError()
            elif i == 0:
                typ = expr_to_unanalyzed_type(
                    arg, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
                )
            elif i == 1:
                name = _extract_argument_name(arg)
            else:
                raise TypeTranslationError()
        return CallableArgument(typ, name, arg_const, expr.line, expr.column)
    elif isinstance(expr, ListExpr):
        return TypeList(
            [
                expr_to_unanalyzed_type(
                    t,
                    options,
                    allow_new_syntax,
                    expr,
                    allow_unpack=True,
                    lookup_qualified=lookup_qualified,
                )
                for t in expr.items
            ],
            line=expr.line,
            column=expr.column,
        )
    elif isinstance(expr, StrExpr):
        return parse_type_string(expr.value, "builtins.str", expr.line, expr.column)
    elif isinstance(expr, BytesExpr):
        return parse_type_string(expr.value, "builtins.bytes", expr.line, expr.column)
    elif isinstance(expr, UnaryExpr):
        # Handle `not` for type booleans
        if expr.op == "not":
            return UnboundType(
                "__builtins__._Not",
                [
                    expr_to_unanalyzed_type(
                        expr.expr, options, allow_new_syntax, lookup_qualified=lookup_qualified
                    )
                ],
                line=expr.line,
                column=expr.column,
            )
        typ = expr_to_unanalyzed_type(
            expr.expr, options, allow_new_syntax, lookup_qualified=lookup_qualified
        )
        if isinstance(typ, RawExpressionType):
            if isinstance(typ.literal_value, int):
                if expr.op == "-":
                    typ.literal_value *= -1
                    return typ
                elif expr.op == "+":
                    return typ
        raise TypeTranslationError()
    elif isinstance(expr, IntExpr):
        return RawExpressionType(expr.value, "builtins.int", line=expr.line, column=expr.column)
    elif isinstance(expr, FloatExpr):
        # Floats are not valid parameters for RawExpressionType , so we just
        # pass in 'None' for now. We'll report the appropriate error at a later stage.
        return RawExpressionType(None, "builtins.float", line=expr.line, column=expr.column)
    elif isinstance(expr, ComplexExpr):
        # Same thing as above with complex numbers.
        return RawExpressionType(None, "builtins.complex", line=expr.line, column=expr.column)
    elif isinstance(expr, EllipsisExpr):
        return EllipsisType(expr.line)
    elif allow_unpack and isinstance(expr, StarExpr):
        # Check if this is a type comprehension: *[Expr for var in T if Cond]
        if isinstance(expr.expr, ListComprehension):
            gen = expr.expr.generator
            # Only support single generator
            if len(gen.sequences) != 1:
                raise TypeTranslationError()
            # The index should be a simple name
            index = gen.indices[0]
            if not isinstance(index, NameExpr):
                raise TypeTranslationError()
            iter_name = index.name
            element_expr = expr_to_unanalyzed_type(
                gen.left_expr, options, allow_new_syntax, lookup_qualified=lookup_qualified
            )
            iter_type = expr_to_unanalyzed_type(
                gen.sequences[0], options, allow_new_syntax, lookup_qualified=lookup_qualified
            )
            conditions: list[Type] = [
                expr_to_unanalyzed_type(
                    cond, options, allow_new_syntax, lookup_qualified=lookup_qualified
                )
                for cond in gen.condlists[0]
            ]
            return TypeForComprehension(
                element_expr=element_expr,
                iter_name=iter_name,
                iter_type=iter_type,
                conditions=conditions,
                line=expr.line,
                column=expr.column,
            )
        return UnpackType(
            expr_to_unanalyzed_type(
                expr.expr, options, allow_new_syntax, lookup_qualified=lookup_qualified
            ),
            from_star_syntax=True,
        )
    elif isinstance(expr, DictExpr):
        if not expr.items:
            raise TypeTranslationError()
        items: dict[str, Type] = {}
        extra_items_from: list[ProperType] = []
        for item_name, value in expr.items:
            if not isinstance(item_name, StrExpr):
                if item_name is None:
                    typ = expr_to_unanalyzed_type(
                        value, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
                    )
                    # TypedDict spread values should be ProperTypes
                    assert isinstance(typ, ProperType)
                    extra_items_from.append(typ)
                    continue
                raise TypeTranslationError()
            items[item_name.value] = expr_to_unanalyzed_type(
                value, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
            )
        result = TypedDictType(
            items, set(), set(), Instance(MISSING_FALLBACK, ()), expr.line, expr.column
        )
        result.extra_items_from = extra_items_from
        return result
    elif isinstance(expr, DictionaryComprehension):
        # Dict comprehension in type context: {k: v for x in foo}
        # desugars to *[_DictEntry[k, v] for x in foo]
        if len(expr.sequences) != 1:
            raise TypeTranslationError()
        index = expr.indices[0]
        if not isinstance(index, NameExpr):
            raise TypeTranslationError()
        iter_name = index.name
        key_type = expr_to_unanalyzed_type(
            expr.key, options, allow_new_syntax, lookup_qualified=lookup_qualified
        )
        value_type = expr_to_unanalyzed_type(
            expr.value, options, allow_new_syntax, lookup_qualified=lookup_qualified
        )
        iter_type = expr_to_unanalyzed_type(
            expr.sequences[0], options, allow_new_syntax, lookup_qualified=lookup_qualified
        )
        cond_types: list[Type] = [
            expr_to_unanalyzed_type(
                cond, options, allow_new_syntax, lookup_qualified=lookup_qualified
            )
            for cond in expr.condlists[0]
        ]
        element_expr = UnboundType(
            "__builtins__._DictEntry", [key_type, value_type], line=expr.line, column=expr.column
        )
        return TypeForComprehension(
            element_expr=element_expr,
            iter_name=iter_name,
            iter_type=iter_type,
            conditions=cond_types,
            line=expr.line,
            column=expr.column,
        )
    elif isinstance(expr, ConditionalExpr):

        # Use __builtins__ so it can be resolved without explicit import
        return UnboundType(
            "__builtins__._Cond",
            [
                expr_to_unanalyzed_type(
                    arg, options, allow_new_syntax, expr, lookup_qualified=lookup_qualified
                )
                for arg in [expr.cond, expr.if_expr, expr.else_expr]
            ],
            line=expr.line,
            column=expr.column,
        )

    else:
        raise TypeTranslationError()
