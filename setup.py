import os
import sys

from setuptools import find_packages, setup

if sys.version_info < (3, 7):
    sys.exit("Python 3.7 is the minimum required version")

PROJECT_ROOT = os.path.dirname(__file__)

with open(os.path.join(PROJECT_ROOT, "README.rst")) as file_:
    long_description = file_.read()

INSTALL_REQUIRES = [
    "h11",
    "h2 >= 3.1.0",
    "priority",
    "toml",
    "typing_extensions >= 3.7.4; python_version < '3.8'",
    "wsproto >= 0.14.0",
]

TESTS_REQUIRE = [
    "hypothesis",
    "mock",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-trio",
    "trio",
]

setup(
    name="nonecorn",
    version="0.14.1dev1",
    python_requires=">=3.7",
    description="A ASGI Server forked from hypercorn with more extra feature beyond ASGI",
    long_description=long_description,
    url="https://gitlab.com/pgjones/hypercorn/",
    author="P G Jones",
    author_email="philip.graham.jones@googlemail.com",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    packages=find_packages("src"),
    package_dir={"": "src"},
    py_modules=["hypercorn"],
    install_requires=INSTALL_REQUIRES,
    extras_require={
        "h3": ["aioquic >= 0.9.0, < 1.0"],
        "tests": TESTS_REQUIRE,
        "trio": ["trio >= 0.11.0"],
        "uvloop": ["uvloop"],
    },
    tests_require="hypercorn[tests]",
    entry_points={"console_scripts": ["hypercorn = hypercorn.__main__:main"]},
    include_package_data=True,
)
