# TODO

## 1. Le mode remote SSH exige une auth non interactive — connexion trop difficile

**Priorité : haute.** C'est le principal frein à l'adoption du mode remote, qui
est pourtant l'argument de vente du README.

### Symptôme

```sh
DOCKER_HOST=ssh://root@164.68.100.31 dsup
```

Sur un hôte en authentification par mot de passe, l'écran reste en état
dégradé et réessaie indéfiniment. Aucun moyen de saisir le mot de passe.
Le même hôte répond pourtant très bien en direct :

```
$ ssh root@164.68.100.31 docker info
root@164.68.100.31's password:
Client: Docker Engine - Community
 Version: 29.2.1
```

### Cause

La connexion s'ouvre **après** que Textual a pris le terminal :

1. `cli.py:58-65` — `connect` est passé à `DsupApp` sans être appelé, puis
   `app.run()` démarre l'interface.
2. `app.py:198` — `on_mount` lance `_poll_loop`.
3. `app.py:214` — `_poll_loop` appelle enfin `connect` via `asyncio.to_thread`,
   alors que l'écran alterné et le mode raw sont déjà actifs.
4. `docker.py:280-283` — `connect()` ouvre **deux** clients (control + stream),
   donc deux processus `ssh`, donc potentiellement deux prompts.

`use_ssh_client=True` délègue au binaire `ssh` du système : tout prompt
interactif (mot de passe, passphrase de clé, confirmation d'hôte inconnu
`Are you sure you want to continue connecting?`) arrive à ce moment-là. Les
frappes partent dans Textual, pas dans `ssh`. La connexion bloque jusqu'au
timeout de 8 s (`docker.py:26`), puis réessaie toutes les 5 s
(`app.py:44`) — en butant à chaque fois sur le même prompt.

À vérifier : `ssh` lit les mots de passe sur `/dev/tty` et non sur stdin, donc
le prompt pourrait s'afficher par-dessus l'interface, avec `ssh` et Textual qui
se disputent le clavier. Comportement non observé, à reproduire.

### Contrainte actuelle (non documentée)

Le mode remote ne fonctionne aujourd'hui que si :

- une clé est chargée dans `ssh-agent`, ou la clé est sans passphrase ;
- l'hôte figure déjà dans `~/.ssh/known_hosts`.

Test préalable qui tranche — c'est déjà l'indice renvoyé par `docker.py:375` :

```sh
ssh user@hôte docker info     # doit passer sans rien taper
```

### Pistes

**A. Connecter avant `app.run()`** *(recommandé)* — appeler `connect()` dans
`main()`, avant que Textual ne prenne le terminal : `ssh` retrouve un terminal
normal et peut demander mot de passe ou passphrase.
`docker.from_env(version="auto")` négocie l'API dès la construction, donc
l'appel touche bien le réseau et déclenche le prompt à cet instant.

Attention à préserver la propriété annoncée dans la docstring de `cli.py` :
« le daemon absent au lancement n'est pas une erreur fatale ». Il faut donc
rattraper `DockerError` et démarrer quand même en état dégradé avec la
fabrique de reconnexion. Ne règle que la **première** connexion : une
reconnexion en cours de session reste sous le TUI.

**B. `App.suspend()` autour de la reconnexion** — disponible en Textual 8.2.8
(vérifié). Restaure le terminal le temps de l'appel, ce qui couvre aussi les
reconnexions de `_poll_loop`, là où A s'arrête. Plus intrusif : clignotement
de l'écran à chaque tentative, à ne déclencher que si le prompt est probable.

**C. `BatchMode=yes`** — en complément, pas en remplacement. Échoue
immédiatement avec un message clair (« clé SSH requise, voir README ») au lieu
d'attendre 8 s puis de boucler en silence. N'aide pas à se connecter, mais
supprime le pire symptôme : l'échec muet.

**D. Multiplexage SSH (`ControlMaster`)** — `connect()` ouvre deux connexions
là où une suffirait à l'authentification. Une socket partagée éviterait le
double prompt et accélérerait le démarrage.

### Aussi

- Documenter le prérequis dans la section « En remote » du README : elle promet
  « votre `~/.ssh/config` s'applique tel quel » sans mentionner que l'auth doit
  être non interactive. Ajouter le test `ssh … docker info`.
- Ajouter un test de régression : passerelle qui lève au premier appel, puis
  réussit — vérifier que l'app démarre en état dégradé sans bloquer.
