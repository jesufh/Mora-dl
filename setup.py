from setuptools import setup, find_packages

setup(
    name="mora-scraper",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "requests>=2.28.0",
        "click>=8.1.0",
        "rich>=13.0.0",
        "pydantic>=2.0.0",
        "mutagen>=1.46.0",
        "tqdm>=4.65.0",
    ],
    entry_points={
        "console_scripts": [
            "mora = mora.cli:cli",
        ],
    },
    author="Jesufh",
    description="Modern scraper to download FLAC music from the hifi API",
    python_requires=">=3.9",
)