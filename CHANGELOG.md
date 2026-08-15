# Journal des versions

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le
versionnage [SemVer](https://semver.org/lang/fr/).

## [Non publié]

## [0.1.0] — 2026-08-15

Première version publiée.

### Ajouté

- Écran unique de supervision : états, CPU, RAM, ports, uptime, logs en direct.
- Actions sur les conteneurs — `start`, `restart`, `stop`, `kill`, `rm` — les
  destructives derrière une confirmation.
- Écran de logs avec suivi temps réel, recherche et retour à la ligne.
- Panneau détail au-delà de 110 colonnes.
- **Mode remote par tunnel SSH** : `dsup user@hôte` ouvre un forwarding de
  socket Unix vers le daemon distant. Rien n'est installé ni exécuté sur le
  serveur — pas même le CLI `docker`, contrairement à la méthode
  `DOCKER_HOST=ssh://…` qui repose sur `docker system dial-stdio`.
  L'authentification n'est demandée qu'une fois, avant l'affichage, et le
  tunnel survit à la fermeture (`dsup --disconnect` y met fin).
- `--remote-socket` pour Docker rootless.
- État dégradé avec reconnexion automatique quand le daemon devient
  injoignable.

### Sécurité

- La socket locale du tunnel donne un accès complet au daemon distant,
  équivalent à root sur ce serveur : `~/.dsup/` est créé en `0700`.
- Les reconnexions automatiques passent en `BatchMode`, donc aucune invite SSH
  ne peut surgir pendant que l'interface occupe le terminal.

[Non publié]: https://github.com/ainayves/docker-tui/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ainayves/docker-tui/releases/tag/v0.1.0
