# django-tastypie-openapi

Generate OpenAPI specification for Django-Tastypie API.

## Usage

```bash
$ pip install django-tastypie-openapi
```

Then add to your urls.py:

```python
...
from tastypie_openapi import SchemaView
...

urlpatterns = [
...
    path('api/schema/', SchemaView.as_view(api=ApiV0, title="API", version="0.0.0")),
...
]
```

Where `ApiV0` is your `tastypie.Api` object.

## Status

This is ALPHA software, may change in the future.

Currently, operations are generated only for the following combinations:

- Get/List
- Post/List
- Get/Detail
- Put/Detail
- Patch/Detail
- Delete/Detail

Other operations are not exported.
