# Emote packs

Drop an emote export here and Kinodemo will render the names in chat as images.

## Getting an export

Emotes live on the CyTube channel, not in Kinodemo. To export them:

1. Open the channel in a browser: `https://<host>/r/<channel>` (e.g.
   `https://cytu.be/r/the-greens-shadowplex`).
2. Open the browser's developer console (F12 → Console).
3. Paste this and press Enter:

   ```js
   JSON.stringify(CHANNEL.emotes.map(function (f) {
       return {
           name: f.name,
           image: f.image
       };
   }));