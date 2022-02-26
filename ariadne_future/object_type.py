from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from graphql import GraphQLResolveInfo
from graphql.language.ast import (
    DefinitionNode,
    FieldDefinitionNode,
    ObjectTypeDefinitionNode,
    ObjectTypeExtensionNode,
)

from .base_type import BaseType
from .dependencies import get_dependencies_from_object_type
from .utils import parse_definition

Dependencies = Tuple[str, ...]
FieldsDict = Dict[str, FieldDefinitionNode]
ObjectNodeType = Union[ObjectTypeDefinitionNode, ObjectTypeExtensionNode]
RequirementsDict = Dict[str, DefinitionNode]

STD_TYPES = ("ID", "Int", "String", "Boolean")


class ObjectTypeMeta(type):
    def __new__(cls, name: str, bases, kwargs: dict):
        if kwargs.pop("__abstract__", False):
            return super().__new__(cls, name, bases, kwargs)

        schema = kwargs.get("__schema__")

        graphql_def = assert_schema_defines_valid_type(
            name, parse_definition(name, schema)
        )
        graphql_fields = extract_graphql_fields(name, graphql_def)

        requirements: RequirementsDict = {
            req.graphql_name: req.graphql_type
            for req in kwargs.setdefault("__requires__", [])
        }

        if isinstance(graphql_def, ObjectTypeExtensionNode):
            assert_requirements_contain_extended_type(name, graphql_def, requirements)

        dependencies = get_dependencies_from_object_type(graphql_def)
        assert_requirements_are_met(name, dependencies, requirements)

        kwargs["graphql_name"] = graphql_def.name.value
        kwargs["graphql_type"] = type(graphql_def)

        aliases = kwargs.setdefault("__aliases__", {})
        assert_aliases_match_fields(name, aliases, graphql_fields)

        defined_resolvers = get_defined_resolvers(kwargs)
        kwargs["_resolvers"] = get_final_resolvers(
            name, graphql_fields, aliases, defined_resolvers
        )

        return super().__new__(cls, name, bases, kwargs)


def assert_schema_defines_valid_type(
    type_name: str, type_def: DefinitionNode
) -> ObjectNodeType:
    if not isinstance(type_def, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
        raise ValueError(
            f"{type_name} class was defined with __schema__ containing invalid "
            f"GraphQL type definition: {type(type_def).__name__} (expected type)"
        )

    return cast(ObjectNodeType, type_def)


def extract_graphql_fields(type_name: str, type_def: ObjectNodeType) -> FieldsDict:
    if not type_def.fields:
        raise ValueError(
            f"{type_name} class was defined with __schema__ containing empty "
            f"GraphQL type definition"
        )

    return {field.name.value: field for field in type_def.fields}


def assert_aliases_match_fields(
    type_name: str, aliases: Iterable[str], fields: Iterable[str]
):
    invalid_aliases = set(aliases) - set(fields)
    if invalid_aliases:
        raise ValueError(
            f"{type_name} class was defined with aliases for fields not in "
            f"GraphQL type: {', '.join(invalid_aliases)}"
        )


def get_defined_resolvers(kwargs: Dict[str, Any]) -> Dict[str, Callable]:
    resolvers = {}
    for name, value in kwargs.items():
        if not name.startswith("resolve_"):
            continue

        if isinstance(value, staticmethod):
            # Fix for py<3.10
            value = value.__get__(object)

        if callable(value):
            resolvers[name[8:]] = value
    return resolvers


def get_final_resolvers(
    type_name: str,
    fields: FieldsDict,
    aliases: Dict[str, str],
    resolvers: Dict[str, Callable],
) -> Dict[str, Callable]:
    used_resolvers = []
    final_resolvers = {}

    for field_name in fields:
        if aliases and field_name in aliases:
            resolver_name = aliases[field_name]
            if resolver_name in resolvers:
                used_resolvers.append(resolver_name)
                final_resolvers[field_name] = resolvers[resolver_name]
            else:
                final_resolvers[field_name] = create_alias_resolver(resolver_name)

        elif field_name in resolvers:
            used_resolvers.append(field_name)
            final_resolvers[field_name] = resolvers[field_name]

    unused_resolvers = [
        f"resolve_{field_name}" for field_name in set(resolvers) - set(used_resolvers)
    ]
    if unused_resolvers:
        raise ValueError(
            f"{type_name} class was defined with resolvers for fields not in "
            f"GraphQL type: {', '.join(unused_resolvers)}"
        )

    return final_resolvers


def create_alias_resolver(field_name: str):
    def default_aliased_field_resolver(
        source: Any, info: GraphQLResolveInfo, **args: Any
    ) -> Any:
        value = (
            source.get(field_name)
            if isinstance(source, Mapping)
            else getattr(source, field_name, None)
        )

        if callable(value):
            return value(info, **args)
        return value

    return default_aliased_field_resolver


def assert_requirements_contain_extended_type(
    type_name: str,
    type_def: ObjectTypeExtensionNode,
    requirements: RequirementsDict,
):
    graphql_name = type_def.name.value
    if graphql_name not in requirements:
        raise ValueError(
            f"{type_name} graphql_type was defined without required GraphQL type "
            f"definition for '{graphql_name}' in __requires__"
        )


def assert_requirements_are_met(
    type_name: str,
    dependencies: Dependencies,
    requirements: RequirementsDict,
):
    for graphql_name in dependencies:
        if graphql_name not in requirements:
            raise ValueError(
                f"{type_name} class was defined without required GraphQL type "
                f"definition for '{graphql_name}' in __requires__"
            )


class ObjectType(BaseType, metaclass=ObjectTypeMeta):
    __abstract__ = True
    __root__: Optional[Any]
    __schema__: str
    __aliases__: Optional[Dict[str, str]]
    __requires__: List[Type[BaseType]]

    graphql_name: str
    graphql_type: ObjectNodeType

    _resolvers: Dict[str, Callable[..., Any]]

    @classmethod
    def __bind_to_schema__(cls, schema):
        graphql_type = schema.type_map.get(cls.graphql_name)

        for field_name, field_resolver in cls._resolvers.items():
            graphql_type.fields[field_name].resolve = field_resolver
