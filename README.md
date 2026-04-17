# Mora-dl
<div align="center">
  <img alt="logo" src="https://i.imgur.com/fnneFa9.png"/></br>
  <img width="343" height="30" alt="image" src="https://github.com/user-attachments/assets/c33bc4c5-b0ef-4a07-a8dc-b25cc2e63c31" /><br>
  <b>A simple and efficient scraper for Tidal to download FLAC music.</b>
</div>
<br>
I love music, and i decided to create a small and efficient application for downloading high-fidelity music in FLAC format. It's basically a scraper like any other.<br><br>
It uses a sophisticated search system provided by the Deezer API and retrieves audio via third-party APIs. It also fetches metadata and embeds it in the audio file, and you can use Telegram as cloud storage if you wish. Enjoy it <3!<br><br>

> [!NOTE]
I borrowed the API from a site i came across called [squidWTF](https://tidal.squid.wtf) (what a beautiful project!), which also inspired me to do this in Python. Because of this, it's not self-sufficient and is susceptible to becoming completely unusable if the endpoints stop working. Check it out at [hifi-api](https://github.com/binimum/hifi-api), credit to him! ^-^.

## Installation
Python 3.10+ is required, and you must use the new Windows terminal if you want emojis to display correctly.

You can install and prepare the project using the `setup.py` command or install the dependencies using `requirements.txt`, whichever you prefer.
```bash
# Using setup.py
pip install -e .

# Install requirements and run the module
pip install -r requirements.txt
```

## Documentation
Use a series of easy-to-use arguments. For reference, you can use `--help`:
```bash
.\mora <--track, --album, --artist, --playlist>
       <--query "entry"> #playlist: spotify url
       [--quality]
       [--outpot "directory"]
       [--backup]
       [--no-backup]
```
For example:
```bash
# Search for an artist
mora --artist -q "Bad Bunny" --quality HI_RES_LOSSLESS

# Search for a specific track
mora --track -q "Monaco" -o "./MyMusic"
```
### Telegram
For storage purposes, i’ve opted to use Telegram cloud service for now. When you start, you’ll be asked for your API HASH and API ID credentials obtained from [Telegram API](https://my.telegram.org) (please do not share these), a unique passphrase that will be used to encrypt and decrypt the audio files, and the chat—whether a channel or group, private or public—that will be used to store the files.

Encryption protects the file from DMCA detection, but it is not entirely immune to manual reporting; if your channel or group is reported, there will be a manual review, and most likely all your content will be deleted.

The Telegram API is strictly monitored and limited; I chose to use a Telegram account rather than a bot because the latter has an upload and download limit of 50 MB per file, but this puts your account at risk due to data transfer limits and potential spam flags. To avoid major issues, it is recommended to create an alternate account using a virtual number (they are inexpensive).
