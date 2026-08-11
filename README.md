# dsup

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

**En remote** — deux façons, toujours sans agent ni serveur :

```sh
# dsup est installé sur le serveur : on s'y connecte et on le lance
ssh prod-eu-1
dsup

# ou dsup tourne en local et parle au daemon distant à travers SSH
DOCKER_HOST=ssh://admin@prod-eu-1 dsup
```

La seconde forme n'installe rien sur la machine distante : `dsup` délègue le
transport au binaire `ssh` du système, donc votre `~/.ssh/config` (alias,
clés, jump hosts) s'applique tel quel.

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
