import copy
import json
import typing
import six
from django.db.models import fields as djangofields
from django.views import View
from django.http.response import HttpResponse
from django.core.exceptions import ImproperlyConfigured, FieldDoesNotExist
from tastypie.api import Api
from tastypie import resources, fields, exceptions

__all__ = ['SchemaView', 'RawForeignKey']

VERSION = "3.0.3"


def fieldToOASType(f: fields.ApiField) -> str:
    if isinstance(f, fields.IntegerField):
        return 'integer'
    if isinstance(f, fields.FloatField):
        return 'number'
    if isinstance(f, fields.BooleanField):
        return 'boolean'

    return 'string'


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (Object, DelayedSchema)):
            return o.serialize()

        return super().default(o)


class Object:
    def __init__(self, content=None):
        self.ref = None
        self.content = content

    def serialize(self):
        if self.ref:
            return {"$ref": self.ref}

        return self.content


class DelayedSchema:
    def __init__(self, cache, name):
        self._cache = cache
        self._name = name

    def serialize(self):
        if self._name in self._cache:
            return self._cache[self._name]

        return {
            "type": "string",
        }


class Schema(Object):
    def __init__(self, title: str, version: str):
        self.title = title
        self.version = version

        self.paths: typing.Mapping[str, typing.typing.Any] = {}
        self.components: typing.Mapping[str, typing.Mapping[str, typing.Any]] = {}

    def _register_component(self, component: str, name: str, object: Object):
        comp = self.components.setdefault(component, {})
        if name in comp:
            raise RuntimeError('/components/{}/{} already exists'.format(component, name))

        path = '#/components/{}/{}'.format(component, name)
        comp[name] = object.serialize()
        object.ref = path

    def register_schema(self, name, schema):
        self._register_component('schemas', name, schema)

    def register_response(self, name, response):
        self._register_component('responses', name, response)

    def register_requestBody(self, name, requestBody):
        self._register_component('requestBodies', name, requestBody)

    def register_parameter(self, name, parameter):
        self._register_component('parameters', name, parameter)

    def serialize(self):
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
    api = None
    title = None
    version = None

    def __init__(self, api: Api, title: str, version: str):
        if not isinstance(api, Api):
            raise ImproperlyConfigured("Invalid api object passed")

        self.api = api
        self.title = title
        self.version = version
        self._schemacache = {}

    def field_to_schema(self, model, tfield):
        if isinstance(tfield, RawForeignKey):
            fk_class = tfield.to_class
            fk_className = fk_class.__name__.replace('Resource', '')
            fk_pkcol = fk_class._meta.object_class._meta.pk.name

            return DelayedSchema(self._schemacache, '{}{}'.format(
                fk_className, fk_pkcol.capitalize()))

        schema = {
            "description": tfield.verbose_name or 'NO_DESCRIPTION',
            "type": fieldToOASType(tfield),
        }
        if tfield.null:
            schema["nullable"] = True

        format = None
        enum = None
        if tfield.attribute is not None:
            try:
                djangofield = model._meta.get_field(tfield.attribute)
                if isinstance(djangofield, djangofields.UUIDField):
                    format = 'uuid'
                elif isinstance(djangofield, djangofields.DateField):
                    format = 'date'
                elif isinstance(djangofield, djangofields.DateTimeField):
                    format = 'date-time'

                if djangofield.choices:
                    enum = [
                        i
                        for i, _ in djangofield.choices
                    ]

            except FieldDoesNotExist:
                pass

        if format:
            schema["format"] = format
        if enum:
            schema["enum"] = enum

        return Object(schema)

    def get(self, request):
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
            endpoint = self.api._build_reverse_url("api_dispatch_list", kwargs={
                'api_name': self.api.api_name,
                'resource_name': name,
            })
            model = cls._meta.object_class

            # process fields
            # collect primary key
            wSchemaName = '{}W'.format(resource_name)
            rSchemaName = '{}R'.format(resource_name)
            primary_key = None
            fieldSchema = {}

            rschema = {
                "type": "object",
                "properties": {},
                "required": [],
            }

            wschema = {
                "type": "object",
                "properties": {},
                "required": [],
            }

            for f, fd in cls.fields.items():
                if f == "resource_uri":
                    continue

                fieldSchema[f] = self.field_to_schema(model, fd)
                fieldName = '{}{}'.format(resource_name, f.capitalize())
                self._schemacache[fieldName] = fieldSchema[f]

                openapischema.register_schema(fieldName, fieldSchema[f])

                if primary_key is None:
                    try:
                        if fd.attribute is not None:
                            df = model._meta.get_field(fd.attribute)
                            if df.primary_key:
                                primary_key = f
                                # continue
                    except FieldDoesNotExist:
                        pass

                s = rschema if fd.readonly else wschema
                if not fd.null:
                    s["required"].append(f)

                s["properties"][f] = fieldSchema[f]

            if wschema["properties"]:
                wSchema = Object(wschema)
                openapischema.register_schema(wSchemaName, wSchema)

            if rschema["properties"]:
                rSchema = Object(rschema)
                openapischema.register_schema(rSchemaName, rSchema)

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

            operations = {}
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
                    "summary": "Get list of {} with filtering".format(resource_name),
                    "operationId": "List{}".format(resource_name),
                    "parameters": params,
                    "responses": {
                        "200": {
                            "description": "List of {}".format(resource_name),
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
                "description": "Values for {}".format(resource_name),
                "content": {
                    "application/json": {
                        "schema": wSchema,
                    },
                }
            })
            openapischema.register_requestBody('create{}'.format(resource_name), requestBody)

            if 'post' in cls._meta.list_allowed_methods:
                op = {
                    "summary": "Create {}".format(resource_name),
                    "operationId": "Create{}".format(resource_name),
                    "requestBody": requestBody,
                    "responses": {
                        "default": {
                            "description": "",
                        },
                        "201": {
                            "description": "{} successfully created".format(resource_name),
                            "headers": {
                                "Location": {
                                    "description": "URI of created {}".format(resource_name),
                                    "schema": {
                                        "type": "string",
                                    },
                                },
                            },
                        },
                    },
                }
                if cls._meta.always_return_data:
                    op["responses"]["201"]["content"] = {
                        "application/json": {
                            "schema": fullSchema,
                        },
                    }

                operations['post'] = op

            if operations:
                openapischema.paths[endpoint] = operations

            # Process detail operations
            if primary_key:
                operations = {}
                idparam = Object({
                    "name": primary_key,
                    "in": "path",
                    "required": True,
                    "schema": fieldSchema[primary_key],
                })
                detailendpoint = '{}{{{}}}/'.format(endpoint, primary_key)

                if 'get' in cls._meta.detail_allowed_methods:
                    operations['get'] = {
                        "summary": "Get a single {} by primary key".format(resource_name),
                        "operationId": "Get{}".format(resource_name),
                        "parameters": [idparam],
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "200": {
                                "description": "{} successfully retrieved".format(resource_name),
                                "content": {
                                    "application/json": {
                                        "schema": fullSchema,
                                    },
                                },
                            },
                            "404": {
                                "description": "{} not found".format(resource_name),
                            }
                        },
                    }

                if 'put' in cls._meta.detail_allowed_methods:
                    op = {
                        "summary": "Overwrite a single {} by primary key".format(resource_name),
                        "operationId": "Put{}".format(resource_name),
                        "parameters": [idparam],
                        "requestBody": requestBody,
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "202": {
                                "description": "{} successfully accepted".format(resource_name),
                            },
                            "404": {
                                "description": "{} not found".format(resource_name),
                            }
                        },
                    }
                    if cls._meta.always_return_data:
                        op["responses"]["202"]["content"] = {
                            "application/json": {
                                "schema": fullSchema,
                            },
                        }

                    operations['put'] = op

                if 'patch' in cls._meta.detail_allowed_methods:
                    patchSchema = Object(copy.deepcopy(wSchema.content))
                    patchSchema.content.pop("required")

                    op = {
                        "summary": "Patch a single {} by primary key".format(resource_name),
                        "operationId": "Patch{}".format(resource_name),
                        "parameters": [idparam],
                        "requestBody": {
                            "required": True,
                            "description": "Values for {}".format(resource_name),
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
                                "description": "{} successfully accepted".format(resource_name),
                            },
                            "404": {
                                "description": "{} not found".format(resource_name),
                            }
                        },
                    }
                    if cls._meta.always_return_data:
                        op["responses"]["202"]["content"] = {
                            "application/json": {
                                "schema": fullSchema,
                            },
                        }

                    operations['patch'] = op

                if 'delete' in cls._meta.detail_allowed_methods:
                    op = {
                        "summary": "Delete a single {} by primary key".format(resource_name),
                        "operationId": "Delete{}".format(resource_name),
                        "parameters": [idparam],
                        "responses": {
                            "default": {
                                "description": "",
                            },
                            "204": {
                                "description": "{} successfully deleted".format(resource_name),
                            },
                            "404": {
                                "description": "{} not found".format(resource_name),
                            }
                        },
                    }

                    operations['delete'] = op

                if operations:
                    openapischema.paths[detailendpoint] = operations

        return HttpResponse(json.dumps(openapischema, cls=JSONEncoder))


class RawForeignKey(fields.ToOneField):
    """
    RawForeignKey exposes raw foreign key values
    """

    def dehydrate(self, bundle, for_list):
        return getattr(bundle.obj, self.attribute + '_id')

    def build_related_resource(self, value, request=None, related_obj=None, related_name=None):
        if isinstance(value, six.string_types):
            fk_resource = self.to_class()

            bundle = fk_resource.build_bundle(request=request)
            bundle.obj = fk_resource.obj_get(bundle=bundle, pk=value)

            return fk_resource.full_dehydrate(bundle)

        return super().build_related_resource(value, request=request, related_obj=related_obj, related_name=related_name)

    @property
    def dehydrated_type(self):
        return resources.BaseModelResource.api_field_from_django_field(
            self.to_class.Meta.object_class._meta.pk).dehydrated_type
