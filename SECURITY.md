# Security

This app handles Apple ID sign-ins and can delete photos from iCloud, so
security reports are taken seriously.

**Please don't open a public issue for a security problem.** Report it
privately instead: on this repository's **Security** tab, choose
**Report a vulnerability**. You'll get a reply there.

Particularly in scope:

- anything that could delete an iCloud item that isn't verified in Immich
- ways for another website or user to reach or drive the wizard
- Apple ID passwords, session cookies or API keys being stored, logged or sent
  anywhere other than Apple and the user's own Immich server
