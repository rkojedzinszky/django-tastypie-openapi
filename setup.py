import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="django-tastypie-openapi",
    version="0.1.0",
    author="Richard Kojedzinszky",
    author_email="richard@kojedz.in",
    description="Generate Openapi specification for Django-Tastypie",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/rkojedzinszky/django-tastypie-openapi",
    py_modules=[
        "django_tastypie_openapi",
    ],
    install_requires=[
        "django",
        "django-tastypie",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
