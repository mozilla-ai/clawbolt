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

Once you connect Google Drive (via the integrations panel), photos are organized into folders by date inside your own Drive under a top-level Clawbolt folder:

```
Job Photos/
  2026-03-15/
    kitchen-before.jpg
    kitchen-after.jpg
  2026-03-16/
    bathroom-tile.jpg
```

If you have not connected Google Drive, the assistant can still read and reply to photos you send, but it will not save them. Connect Drive (via the integrations panel or by asking the assistant) to turn on saving and retrieval. See [Google Drive Setup](https://github.com/mozilla-ai/clawbolt/blob/main/docs/self-host/google-drive-setup.md) for self-host operators.
