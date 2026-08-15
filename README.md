# dsup

[![CI](https://github.com/ainayves/docker-tui/actions/workflows/ci.yml/badge.svg)](https://github.com/ainayves/docker-tui/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dsup)](https://pypi.org/project/dsup/)
[![Python](https://img.shields.io/pypi/pyversions/dsup)](https://pypi.org/project/dsup/)
[![Licence](https://img.shields.io/pypi/l/dsup)](LICENSE)

Surveiller un hôte Docker éparpille l'essentiel sur trois commandes : `docker ps`
en boucle, `docker stats` qui bloque, `docker logs -f` dans un autre onglet.
`dsup` réunit tout dans un seul écran de terminal — états, CPU, RAM, ports,
uptime, logs en direct, actions — avec une seule commande, aucun serveur,
aucun login, aucune configuration.

```
dsup · local                                            maj. 2 s · 7 conteneurs
  ÉTAT        NOM            IMAGE                      CPU   RAM PORTS      UP
▸ ● up        api            ghcr.io/acme/api:1.4      2.1%  184M 8080     3d2h
  ● up        worker         ghcr.io/acme/worker:1.4  11.4%  312M —        3d2h
  ● up        postgres       postgres:16.3             0.8%   96M 5432      12d
  ● up        redis          redis:7.2-alpine          0.3%   18M 6379      12d
  ● up        caddy          caddy:2.8                 0.1%   24M 80,443    12d
  ◌ exited    cron           acme/cron:1.2                —     — —           —
  ✖ err 137   batch          acme/batch:0.9               —     — —           —
~
~
j/k nav  ↵ logs  / filtre  s stop  r rest.  x kill  d rm  ? aide     ● 5 ◌ 1 ✖ 1
```

## Installation

```sh
pipx install dsup
# ou
uv tool install dsup
```

Python 3.10+. Les couleurs sont des tons ANSI moyens, lisibles sur thème clair
comme sombre ; aucun fond n'est peint, `dsup` hérite du thème du terminal.

## Usage

**En local** — `dsup` parle au socket Docker de la machine :

```sh
dsup                     # conteneurs en marche, rafraîchis toutes les 2 s
dsup --all               # inclut les conteneurs arrêtés
dsup --refresh 5         # rythme de rafraîchissement
dsup --filter api        # filtre initial sur le nom ou l'image
```

**En remote** — passez l'hôte en argument :

```sh
dsup admin@prod-eu-1     # user@hôte
dsup prod-eu-1           # ou un alias ~/.ssh/config
dsup --disconnect prod-eu-1
```

**Rien n'est installé ni même exécuté sur le serveur.** `dsup` ouvre un tunnel
SSH vers la socket Docker distante et lui parle à travers une socket locale :

```
ssh -fN -L ~/.dsup/<id>.sock:/var/run/docker.sock prod-eu-1
```

Aucune commande ne tourne en face — pas même le CLI `docker`, que la méthode
habituelle (`DOCKER_HOST=ssh://…`, qui repose sur `docker system dial-stdio`)
exige pourtant sur le serveur. Votre `~/.ssh/config` s'applique tel quel :
alias, clés, jump hosts.

**Le mot de passe n'est demandé qu'une fois.** Le tunnel s'ouvre avant que
l'interface ne prenne le terminal, donc `ssh` peut réclamer un mot de passe,
une passphrase ou la confirmation d'un hôte inconnu — et vous pouvez répondre,
sans limite de temps. Le tunnel survit ensuite à `dsup` : les lancements
suivants démarrent instantanément, sans rien demander, jusqu'à
`dsup --disconnect`. Une clé (`ssh-copy-id admin@prod-eu-1`) supprime la
question définitivement.

Une fois l'interface lancée, plus aucune connexion ne peut poser de question :
les reconnexions automatiques passent en `BatchMode` et échouent proprement
plutôt que de réclamer un mot de passe par-dessus l'affichage.

Docker rootless expose sa socket ailleurs :
`dsup prod-eu-1 --remote-socket /run/user/1000/docker.sock`.

> **Sécurité** — la socket locale donne un accès complet au daemon distant,
> équivalent à root sur ce serveur. `~/.dsup/` est donc créé en `0700`.

Sur un terminal d'au moins 110 colonnes, un panneau détail s'ouvre à droite
(id, image, santé, politique de restart, RAM utilisée / limite, fil de logs) —
rien de nouveau à apprendre, tout devient simplement visible sans `↵`.

## Raccourcis

| Touche | Écran principal | | Touche | Écran logs |
|---|---|---|---|---|
| `j` / `k` | ligne suivante / précédente (ou `↓`/`↑`) | | `j` / `k` | défiler |
| `g` / `G` | début / fin de liste | | `g` / `G` | début / fin (G reprend le suivi) |
| `/` | filtrer par nom ou image · `Esc` efface | | `/` | chercher dans le tampon |
| `↵` | logs du conteneur sélectionné | | `n` / `N` | résultat suivant / précédent |
| `S` | start *(sans confirmation)* | | `f` | suivi temps réel on/off |
| `r` | restart *(sans confirmation)* | | `w` | retour à la ligne on/off |
| `s` | stop — bandeau `y` confirme | | `↵` / `q` | retour |
| `x` | kill — bandeau `y` confirme | | | |
| `d` | rm — bandeau `y` confirme | | | |
| `R` | rafraîchir / réessayer maintenant | | | |
| `?` | rappel des touches secondaires | | | |
| `q` | quitter | | | |

Les actions destructives (`s`, `x`, `d`) affichent un bandeau rouge inversé :
`y` confirme, toute autre touche annule. Défiler vers le haut dans les logs
coupe le suivi ; `G` ou `f` le reprennent.

## Comportement à savoir

- La liste s'affiche immédiatement ; CPU et RAM se remplissent au fur et à
  mesure (le premier relevé CPU affiche `—`, le calcul se fait en delta).
- Daemon injoignable ou connexion perdue : l'écran passe en état dégradé et
  réessaie automatiquement toutes les 5 s (`R` force l'essai).
- Le tampon de logs est borné (2 000 lignes) : un conteneur très bavard ne
  sature pas la mémoire.
- `Ctrl-C` et `SIGTERM` restaurent proprement le terminal.

## Développement

```sh
git clone https://github.com/<vous>/dsup && cd dsup
python3 -m venv venv
source venv/bin/activate          # fish : source venv/bin/activate.fish
pip install -e ".[dev]"
```

Deux pièges se cachent dans ces quatre lignes, et ils n'ont rien à voir avec
`dsup` lui-même.

**L'activation dépend de votre shell.** `venv/bin/activate` est un script POSIX
shell : fish ne partage pas cette syntaxe et échoue sur `case "$(uname)" in`.
Le venv livre un script par shell — `activate.fish` pour fish,
`activate.csh` pour csh/tcsh, `Activate.ps1` pour PowerShell. Chargez celui de
votre shell, pas l'autre.

**`pip install -e ".[dev]"` n'est pas optionnel.** Installer les seules
dépendances ne suffit pas : `dsup` est un point d'entrée déclaré dans
`[project.scripts]`, et l'exécutable `venv/bin/dsup` n'existe qu'une fois le
paquet lui-même installé. Sans cette étape, le venv est activé, `textual` et
`docker` sont présents, et pourtant la commande reste introuvable :

```
(venv) $ dsup
fish: Unknown command: dsup
```

Le `-e` installe en mode éditable — un lien vers vos sources, donc vos
modifications dans `dsup/` sont prises en compte sans réinstaller. Le suffixe
`[dev]` ajoute pytest et mypy ; sans lui, `python -m pytest` répondra
`No module named pytest`.

Les fichiers `requirements.txt` et `requirements-dev.txt` reprennent ces mêmes
dépendances pour les outils qui les attendent, mais `pyproject.toml` reste la
source de vérité : les trois sont à resynchroniser à la main.

### Vérifier

```sh
pytest                   # aucun daemon Docker, aucun accès réseau
mypy                     # strict, sur dsup/ et tests/
dsup --version
```

La suite de tests n'a besoin d'aucun conteneur : la couche Docker est isolée
derrière un protocole et les tests injectent une passerelle factice
(`FakeGateway` dans `tests/conftest.py`). `subprocess.run` est remplacé partout
où `dsup/tunnel.py` lance `ssh`. Une suite qui dépasse quelques secondes
signale donc qu'un test tente une vraie connexion — c'est le symptôme à
surveiller en ajoutant des tests ici.

La CI rejoue tout cela sur Python 3.10 à 3.13, plus macOS, et vérifie en outre
le point d'entrée (`dsup --version`) que la suite n'exerce jamais puisqu'elle
importe `main()` directement.

### Publier une version

La version est écrite **à un seul endroit**, `dsup/__init__.py` : hatch l'y
lit, et la CI refuse de publier si le tag ne lui correspond pas. C'est le
garde-fou qui compte, car un numéro de version consommé sur PyPI ne peut
jamais être réutilisé.

```sh
# 1. modifier __version__ dans dsup/__init__.py
# 2. ajouter l'entrée correspondante dans CHANGELOG.md
git commit -am "version 0.2.0"
git tag v0.2.0
git push origin master --tags
```

Le tag déclenche `release.yml`, qui rejoue mypy et les tests, construit, puis
publie. Pour un essai sans conséquence, lancer le même workflow à la main en
choisissant `testpypi` — c'est la seule répétition possible avant le premier
envoi réel.
