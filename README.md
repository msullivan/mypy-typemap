# PEP 827: Type-Level Computation — Prototype Implementation

This is a prototype implementation of [PEP
827](https://peps.python.org/pep-0827/) (Type Manipulation) in mypy.

The PEP introduces type-level computation operators for introspecting and constructing types.

Most of the main features are prototyped, and this should be suitable
for experimentation, but it it not yet production quality or ready to
be a PR yet. (This prototype has been AI assisted, and at least some
slop has made it in that will need to be fixed; it might benefit from
a full history rewrite, also.)

For the original mypy README, see [REAL_README.md](REAL_README.md).

## What's implemented

- **Type operators**: `IsAssignable`, `IsEquivalent`, `GetArg`, `GetArgs`, `GetMember`, `GetMemberType`, `Members`, `Attrs`, `FromUnion`, `Length`, `Slice`, `Concat`, `Uppercase`, `Lowercase`, `Capitalize`, `Uncapitalize`, `RaiseError`
- **Conditional types**: `true_type if BoolType else false_type`
- **Type-level comprehensions**: `*[T for x in ...]` with filtering
- **Dot notation**: `member.name`, `member.type`, `param.type`, etc.
- **Boolean operators**: `and`, `or`, `not` on type booleans
- **Data types**: `Member[name, type, quals, init, definer]`, `Param[name, type, quals]`
- **Extended callables**: `Callable[Params[Param[...], ...], RetType]`
- **Object construction**: `NewProtocol[*Members]`, `NewTypedDict[*Members]`
- **Class modification**: `UpdateClass[*Members]` as return type of decorators / `__init_subclass__`
- **InitField**: Keyword argument capture with literal type inference
- **Callable introspection**: `GetArg[SomeCallable, Callable, Literal[0]]` returns `Param` types
- Incremental mode

## What's not yet implemented

- `GetSpecialAttr[T, Attr]` — extract `__name__`, `__module__`, `__qualname__`
- `GenericCallable[Vs, lambda <vs>: Ty]` — generic callable types with lambda binding
- `Overloaded[*Callables]` — overloaded function type construction
- `any(comprehension)` / `all(comprehension)` — quantification over type booleans
- `classmethod`/`staticmethod` representation in type-level computation

- Any attempt to make it perform well
- Fine-grained mode is currently broken

## Known bugs

- **`IsAssignable`/`IsEquivalent` over-accept on parameterized tuples.** Both operators ignore type arguments on `tuple` (and presumably other generics), so e.g. `IsAssignable[tuple[Literal[2], Literal[1]], tuple[Literal[2], ...]]` and `IsEquivalent[tuple[Literal[2], Literal[1]], tuple[Literal[2], Literal[2]]]` both return `Literal[True]`. On bare `Literal` values the operators work correctly. (Note: even if implemented correctly, these checks should always return `False` for a fixed-length tuple vs. a homogeneous tuple — neither current behavior is what's needed, so this is a doubly-bad primitive for "all elements equal" style checks. Use `Union[*xs]` collapse against a representative instead.)

## Key files

- `mypy/typelevel.py` — All type operator evaluation logic
- `mypy/typeanal.py` — Desugaring of conditional types, comprehensions, dot notation, extended callables
- `mypy/typeshed/stdlib/_typeshed/typemap.pyi` — Stub declarations for all operators and data types
- `test-data/unit/check-typelevel-*.test` — Test suite

## Some implementation notes

- Evaluating `NewProtocol` creates a new anonymous `TypeInfo` that
  doesn't go into any symbol tables. That `TypeInfo` hangs on to the
  `NewProtocol` invocation that created it, and when we serialize
  `Instance`s that refer to it, we serialize the `NewProtocol`
  invocation instead. This allows us to avoid needing to serialize the
  anonymous `TypeInfo`s.
