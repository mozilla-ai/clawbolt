# Photos & Files

Send a photo to your assistant and it will describe what it sees, store it, and organize it in your cloud storage.

## Sending a photo

Send a photo like you'd send it to anyone else:

```
You: [sends photo of kitchen before demo]

Clawbolt: I can see a dated kitchen with dark wood cabinets,
          laminate countertops, and vinyl flooring. Looks like
          a full gut job. I've saved this to your Job Photos
          folder under today's date.
```

Great for before/after documentation, damage records, material identification, and progress tracking.

## Where photos are stored

With cloud storage connected (Dropbox or Google Drive), photos are organized into folders by date:

```
Job Photos/
  2026-03-15/
    kitchen-before.jpg
    kitchen-after.jpg
  2026-03-16/
    bathroom-tile.jpg
```

Without cloud storage, photos are kept on the server's local storage. Cloud storage is configured by whoever set up your Clawbolt instance. See [Storage Providers](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/storage.md) for technical details.
