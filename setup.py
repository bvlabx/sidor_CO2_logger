from setuptools import setup, find_packages

setup(
    name="sidor-logger",
    version="0.1.0",
    description="AK-protocol logger and live plotter for SICK SIDOR gas analyzers",
    packages=find_packages(),
    package_data={"sidor_logger": ["assets/*.png"]},
    include_package_data=True,
    install_requires=[
        "pyserial>=3.5",
        "matplotlib>=3.5",
    ],
    entry_points={
        "console_scripts": [
            "sidor-logger=sidor_logger.app:main",
        ],
    },
    python_requires=">=3.9",
)
