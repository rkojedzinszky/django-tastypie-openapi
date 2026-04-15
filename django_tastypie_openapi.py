import copy
import json
from typing import Any, Dict, Optional, Type, Union
from django.db.models import fields as djangofields
from django.db.models import Model
from django.views import View
from django.http.response import HttpResponse
from django.core.exceptions import ImproperlyConfigured, FieldDoesNotExist
from tastypie.api import Api
from tastypie import resources, fields
from tastypie.bundle import Bundle

__all__ = ['SchemaView', 'RawForeignKey']

VERSION = "3.0.3"

# Tastypie's internal resource_uri field
_TASTYPIE_RESOURCE_URI_FIELD = 'resource_uri'


def to_camelcase(s: str) -> str:
    return ''.join(i.capitalize() for i in s.split('_') if i)


def fieldToOASType(f: fields.ApiField) -> str:
    """Convert a Tastypie field to an OpenAPI type string."""
    if isinstance(f, fields.IntegerField):
        return 'integer'
    if isinstance(f, fields.FloatField):
        return 'number'
    if isinstance(f, fields.DecimalField):
        return 'number'
    if isinstance(f, fields.BooleanField):
        return 'boolean'
    if isinstance(f, fields.ListField):
        return 'array'
    if isinstance(f, fields.DictField):
        return 'object'

    return 'string'


class JSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles Object, DelayedSchema and Schema serialization."""

    def default(self, o: Any) -> Any:
        if isinstance(o, (Object, DelayedSchema, Schema)):
            return o.serialize()

        return super().default(o)


class Object:
    """Represents an OpenAPI schema object that can be referenced."""

    def __init__(self, content: Dict[str, Any]) -> None:
        self.ref: Optional[str] = None
        self.content = content

    def serialize(self) -> Dict[str, Any]:
        if self.ref:
            return {"$ref": self.ref}

        return self.content


class DelayedSchema:
    """Schema that resolves its content from a cache at serialization time."""

    def __init__(self, cache: Dict[str, Any], name: str) -> None:
        self._cache = cache
        self._name = name

    def serialize(self) -> Dict[str, Any]:
        if self._name in self._cache:
            return self._cache[self._name]

        return {
            "type": "string",
        }


class Schema:
    def __init__(self, title: str, version: str) -> None:
        self.title = title
        self.version = version

        self.paths: Dict[str, Any] = {}
        self.components: Dict[str, Dict[str, Any]] = {}

    def _register_component(self, component: str, name: str, obj: Object) -> None:
        comp = self.components.setdefault(component, {})
        if name in comp:
            raise RuntimeError(f'/components/{component}/{name} already exists')

        path = f'#/components/{component}/{name}'
        comp[name] = obj.serialize()
        obj.ref = path

    def register_schema(self, name: str, schema: Object) -> None:
        self._register_component('schemas', name, schema)

    def register_response(self, name: str, response: Object) -> None:
        self._register_component('responses', name, response)

    def register_requestBody(self, name: str, requestBody: Object) -> None:
        self._register_component('requestBodies', name, requestBody)

    def register_parameter(self, name: str, parameter: Object) -> None:
        self._register_component('parameters', name, parameter)

    def serialize(self) -> Dict[str, Any]:
        return {
            "openapi": VERSION,
            "info": {
                "title": self.title,
                "version": self.version,
            },
            "paths": self.paths,
            "components": self.components,
        }


class SchemaView(View):
    api: Optional[Api] = None
    title: Optional[str] = None
    version: Optional[str] = None

    def __init__(self, api: Api, title: str, version: str) -> None:
        if not isinstance(api, Api):
            raise ImproperlyConfigured("Invalid api object passed")

        self.api = api
        self.title = title
        self.version = version
        self._schemacache: Dict[str, Any] = {}

    def field_to_schema(
        self, model: Optional[Type[Model]], tfield: fields.ApiField
    ) -> Union[Object, DelayedSchema]:
        """Convert a Tastypie field to an OpenAPI schema object."""
        if isinstance(tfield, RawForeignKey):
            fk_class = tfield.to_class
            fk_className = fk_class.__name__.replace('Resource', '')
            fk_pkcol = fk_class._meta.object_class._meta.pk.name

            return DelayedSchema(
                self._schemacache,
                f'{fk_className}{to_camelcase(fk_pkcol)}'
            )

        if isinstance(tfield, fields.ToManyField):
            fk_class = tfield.to_class
            fk_className = fk_class.__name__.replace('Resource', '')

            return Object({
                "type": "array",
                "items": DelayedSchema(
                    self._schemacache,
                    f'{fk_className}{to_camelcase(_TASTYPIE_RESOURCE_URI_FIELD)}'
                ),
            })

        if isinstance(tfield, fields.ToOneField):
            fk_class = tfield.to_class
            fk_className = fk_class.__name__.replace('Resource', '')

            return DelayedSchema(
                self._schemacache,
                f'{fk_className}{to_camelcase(_TASTYPIE_RESOURCE_URI_FIELD)}'
            )

        description = tfield.verbose_name if tfield.verbose_name else ''
        schema: Dict[str, Any] = {
            "description": description,
            "type": fieldToOASType(tfield),
        }
        if tfield.null:
            schema["nullable"] = True

        field_format: Optional[str] = None
        enum: Optional[list] = None
        if model and tfield.attribute is not None:
            try:
                djangofield = model._meta.get_field(tfield.attribute)
                if isinstance(djangofield, djangofields.UUIDField):
                    field_format = 'uuid'
                elif isinstance(djangofield, djangofields.DateTimeField):
                    field_format = 'date-time'
                elif isinstance(djangofield, djangofields.DateField):
                    field_format = 'date'
                elif isinstance(djangofield, djangofields.TimeField):
                    field_format = 'time'
                elif isinstance(djangofield, djangofields.EmailField):
                    field_format = 'email'
                elif isinstance(djangofield, djangofields.URLField):
                    field_format = 'uri'

                if djangofield.choices:
                    enum = [i for i, _ in djangofield.choices]

            except FieldDoesNotExist:
                pass

        if field_format:
            schema["format"] = field_format
        if enum:
            schema["enum"] = enum

        return Object(schema)

    def get(self, request) -> HttpResponse:
        openapischema = Schema(title=self.title, version=self.version)

        listmeta = Object({
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                },
                "offset": {
                    "type": "integer",
                },
                "total_count": {
                    "type": "integer",
                }
            },
        })

        openapischema.register_schema('ListMeta', listmeta)

        for name, cls in self.api._registry.items():
            resource_name = cls.__class__.__name__.replace('Resource', '')
            location_schema: Any = {'type': 'string'}
            endpoint = self.api._build_reverse_url("api_dispatch_list", kwargs={
                'api_name': self.api.api_name,
                'resource_name': name,
            })
            model = cls._meta.object_class

            # process fields
            # collect primary key
            wSchemaName = f'{resource_name}W'
            rSchemaName = f'{resource_name}R'
            primary_key: Optional[str] = None
            notnull_unique_key: Optional[str] = None
            fieldSchema: Dict[str, Any] = {}

            rschema: Dict[str, Any] = {
                "type": "object",
                "properties": {},
                "required": [],
            }

            wschema: Dict[str, Any] = {
                "type": "object",
                "properties": {},
                "required": [],
            }

            for f, fd in cls.fields.items():
                fieldSchema[f] = self.field_to_schema(model, fd)
                if f == _TASTYPIE_RESOURCE_URI_FIELD:
                    location_schema = fieldSchema[f]
                fieldName = f'{resource_name}{to_camelcase(f)}'
                self._schemacache[fieldName] = fieldSchema[f]

                openapischema.register_schema(fieldName, fieldSchema[f])

                if primary_key is None:
                    try:
                        if model and fd.attribute is not None:
                            df = model._meta.get_field(fd.attribute)
                            if df.primary_key:
                                primary_key = f
                                # continue
                    except FieldDoesNotExist:
                        pass

                if notnull_unique_key is None:
                    if fd.unique and not fd.null:
                        notnull_unique_key = f

                s = rschema if fd.readonly else wschema
                if not fd.null:
                    s["required"].append(f)

                s["properties"][f] = fieldSchema[f]

            primary_key = primary_key or notnull_unique_key

            wSchema = Object(wschema)
            if wschema["properties"]:
                openapischema.register_schema(wSchemaName, wSchema)

            rSchema = Object(rschema)
            if rschema["properties"]:
                openapischema.register_schema(rSchemaName, rSchema)

            # Determine fullSchema and fullSchemaName based on available schemas
            fullSchema: Optional[Object] = None
            fullSchemaName: Optional[str] = None

            if wschema["properties"] and rschema["properties"]:
                # Combine rSchema and wSchema
                fullSchemaName = resource_name
                fullSchema = Object({
                    "allOf": [
                        rSchema,
                        wSchema,
                    ]
                })
                openapischema.register_schema(fullSchemaName, fullSchema)

            elif wschema["properties"]:
                fullSchemaName = wSchemaName
                fullSchema = wSchema

            elif rschema["properties"]:
                fullSchemaName = rSchemaName
                fullSchema = rSchema

            # Skip resource if no schema is available
            if fullSchema is None:
                continue

            operations: Dict[str, Any] = {}
            if 'get' in cls._meta.list_allowed_methods:
                params = []
                for f, op in cls._meta.filtering.items():
                    params.append(Object({
                        "name": f,
                        "in": "query",
                        "required": False,
                        "schema": fieldSchema[f],
                    }))

                operations['get'] = {
                    "summary": f"Get list of {resource_name} with filtering",
                    "operationId": f"List{resource_name}",
                    "parameters": params,
                    "responses": {
                        "200": {
                            "description": f"List of {resource_name}",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "meta": listmeta,
                                            "objects": {
                                                "type": "array",
                                                "items": fullSchema,
                                            },
                                        },
                                        "required": ["meta", "objects"],
                                    },
                                },
                            },
                        },
                    },
                }

            requestBody = Object({
                "required": True,
                "description": f"Values for {resource_name}",
                "content": {
                    "application/json": {
                        "schema": wSchema,
                    },
                }
            })
            openapischema.register_requestBody(f'create{resource_name}', requestBody)

            if 'post' in cls._meta.list_allowed_methods:
                op_post: Dict[str, Any] = {
                    "summary": f"Create {resource_name}",
                    "operationId": f"Create{resource_name}",
                    "requestBody": requestBody,
                    "responses": {
                        "default": {
                            "description": "",
                        },
                        "201": {
                            "description": f"{resource_name} successfully created",
                            "headers": {
                                "Location": {
                                    "description": f"URI of created {resource_name}",
                                    "schema": location_schema,
                                },
                            },
                        },
                    },
                }
                if cls._meta.always_return_data:
                    op_post["responses"]["201"]["content"] = {
                        "application/json": {
                            "schema": fullSchema,
                        },
                    }

                operations['post'] = op_post

            if operations:
                openapischema.paths[endpoint] = operations

            # Process detail operations
            if primary_key:
                detail_operations: Dict[str, Any] = {}
                idparam = Object({
                    "name": primary_key,
                    "in": "path",
                    "required": True,
                    "schema": fieldSchema[primary_key],
                })
                detailendpoint = f'{endpoint}{{{primary_key}}}/'

                if 'get' in cls._meta.detail_allowed_methods:
                    detail_operations['get'] = {
                        "summary": f"Get a single {resource_name} by primary key",
                        "operationId": f"Get{resource_name}",
                        "parameters": [idparam],
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "200": {
                                "description": f"{resource_name} successfully retrieved",
                                "content": {
                                    "application/json": {
                                        "schema": fullSchema,
                                    },
                                },
                            },
                            "404": {
                                "description": f"{resource_name} not found",
                            }
                        },
                    }

                if 'put' in cls._meta.detail_allowed_methods:
                    op_put: Dict[str, Any] = {
                        "summary": f"Overwrite a single {resource_name} by primary key",
                        "operationId": f"Put{resource_name}",
                        "parameters": [idparam],
                        "requestBody": requestBody,
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "202": {
                                "description": f"{resource_name} successfully accepted",
                            },
                            "404": {
                                "description": f"{resource_name} not found",
                            }
                        },
                    }
                    if cls._meta.always_return_data:
                        op_put["responses"]["202"]["content"] = {
                            "application/json": {
                                "schema": fullSchema,
                            },
                        }

                    detail_operations['put'] = op_put

                if 'patch' in cls._meta.detail_allowed_methods:
                    patchSchema = Object(copy.deepcopy(wSchema.content))
                    patchSchema.content.pop("required")

                    op_patch: Dict[str, Any] = {
                        "summary": f"Patch a single {resource_name} by primary key",
                        "operationId": f"Patch{resource_name}",
                        "parameters": [idparam],
                        "requestBody": {
                            "required": True,
                            "description": f"Values for {resource_name}",
                            "content": {
                                "application/json": {
                                    "schema": patchSchema,
                                },
                            }
                        },
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "202": {
                                "description": f"{resource_name} successfully accepted",
                            },
                            "404": {
                                "description": f"{resource_name} not found",
                            }
                        },
                    }
                    if cls._meta.always_return_data:
                        op_patch["responses"]["202"]["content"] = {
                            "application/json": {
                                "schema": fullSchema,
                            },
                        }

                    detail_operations['patch'] = op_patch

                if 'delete' in cls._meta.detail_allowed_methods:
                    op_delete: Dict[str, Any] = {
                        "summary": f"Delete a single {resource_name} by primary key",
                        "operationId": f"Delete{resource_name}",
                        "parameters": [idparam],
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "204": {
                                "description": f"{resource_name} successfully deleted",
                            },
                            "404": {
                                "description": f"{resource_name} not found",
                            }
                        },
                    }

                    detail_operations['delete'] = op_delete

                if detail_operations:
                    openapischema.paths[detailendpoint] = detail_operations

        return HttpResponse(
            content=json.dumps(openapischema, cls=JSONEncoder),
            headers={'Content-Type': 'application/json'},
        )


class RawForeignKey(fields.ToOneField):
    """
    RawForeignKey exposes raw foreign key values instead of resource URIs.
    """

    def dehydrate(self, bundle: Bundle, for_list: bool) -> Any:
        """Return the raw foreign key ID value."""
        return getattr(bundle.obj, f'{self.attribute}_id')

    def build_related_resource(self, value: Any, request) -> Any:
        """Build and return the dehydrated related resource."""
        fk_resource = self.to_class()

        bundle = fk_resource.build_bundle(request=request)
        bundle.obj = fk_resource.obj_get(bundle=bundle, pk=value)

        return fk_resource.full_dehydrate(bundle)

    @property
    def dehydrated_type(self) -> str:
        """Return the dehydrated type based on the related model's primary key."""
        to_class = self.to_class

        # Get the object class from Meta
        if hasattr(to_class, 'Meta') and hasattr(to_class.Meta, 'object_class'):
            object_class = to_class.Meta.object_class
        else:
            return 'string'  # Fallback if we can't determine the type

        # Get the primary key field
        pk_field = getattr(object_class._meta, 'pk', None)
        if pk_field is None:
            return 'string'  # Fallback if no primary key

        api_field = resources.BaseModelResource.api_field_from_django_field(pk_field)
        return api_field.dehydrated_type
