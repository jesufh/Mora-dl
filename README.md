# Mora-dl
<div align="center">
  <img width="150" height="150" alt="logo" src="https://github.com/user-attachments/assets/d1fdc2e7-ea03-4666-bd3c-9ecdb20afd33"/></br> 
  <b>A simple and efficient scraper for Tidal to download FLAC music.</b>
</div>
<br>
I love music, and I decided to create a small, intuitive, and efficient application for downloading high-fidelity music in FLAC format (16/24-bit). It's basically a scraper like any other.<br><br>
It uses a sophisticated search system via third-party APIs, including Apple Music (iTunes), to obtain the most accurate results possible. It also retrieves metadata and embeds it within the audio file. Enjoy <3!

> [!NOTE]
I borrowed the API from a site I came across called [squidWTF](https://tidal.squid.wtf) (what a beautiful project!), which also inspired me to do this in Python. Because of this, it's not self-sufficient and is susceptible to becoming completely unusable if the endpoints stop working. Check it out at [hifi-api](https://github.com/binimum/hifi-api), credit to him! ^-^.

## Installation
Python 3.10+ is required, and you must use the new Windows terminal if you want emojis to display correctly.

You can install and prepare the project using the `setup.py` command or install the dependencies using `requirements.txt`, whichever you prefer.
```bash
pip install -e .
```
```bash
pip install -r requirements.txt
```

## Documentation
Use a series of easy-to-use arguments. For reference, you can use `--help`:

`--track:` Search for a song by an artist.<br>
`--album:` Search for an album by an artist.<br>
`--artist:` Search for songs by artist.<br>
`--query:` Search for a term associated with the search type (track, album, or artist).<br>
`--quality:` Select the audio quality (LOSSLESS or HI_RES_LOSSLESS; default=LOSSLESS).<br>
`--output:` Select the output folder for the downloaded files.<br>

<b>To use it, you use the required command `download` + `options [arguments]`. For example:</b>
```bash
# Search for an artist
mora download --artist -q "Bad Bunny" --quality HI_RES_LOSSLESS

# Search for a specific track
mora download --track -q "Monaco" -o "./MyMusic"
```
<img width="1115" height="628" alt="image" src="https://github.com/user-attachments/assets/1975d5f4-7aae-4707-b7d7-9045346f6d99" />
