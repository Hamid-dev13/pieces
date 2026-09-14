# INTENT — pieces

Ce qui est construit, dans quel ordre, et à quoi on reconnaît que c'est fait.
`README.md` dit **quoi** et **pourquoi** ; ce fichier dit **jusqu'où**.

## Le but

Ranger mes documents personnels sans y penser, et les retrouver en les
demandant en français, depuis Telegram.

Le volume est faible et l'arrivée ponctuelle : quelques documents de temps en
temps, pas un flux. Ça écarte d'emblée l'import en masse, la pagination, et
toute optimisation de stockage.

---

## V1

**Critère central — la V1 est finie quand :** j'envoie un PDF au bot, il est
classé sans que j'intervienne, je le redemande en français plus tard, et je le
reçois.

Les stories ci-dessous décomposent ce critère et ajoutent ce qui le rend
utilisable au quotidien.

### sec-1 — Le bot ne répond qu'à moi

Une whitelist de `chat_id`, vérifiée avant tout traitement.

**Done when :** un message envoyé depuis un autre compte Telegram ne déclenche
aucune action et n'apparaît pas dans le stockage. Vérifié en essayant depuis un
second compte, pas en relisant le code.

### ing-1 — Recevoir et stocker un PDF

Le fichier est écrit sur un volume, jamais dans Git. Un document déjà présent
(même empreinte SHA-256) n'est pas stocké deux fois.

**Done when :** le même PDF envoyé deux fois donne une seule entrée, et le bot
le dit plutôt que de rester muet.

### ext-1 — Extraire le texte

`pdftotext` sur les PDF nativement textuels.

**Done when :** sur cinq documents réels de types différents, le texte extrait
contient les informations qui permettent de les distinguer.

### ext-2 — Lire aussi les scans

Quand l'extraction rend trop peu de texte, bascule sur l'OCR (français +
anglais).

**Done when :** un document scanné, sans couche texte, est classé correctement.

### cls-1 — Classer automatiquement

Le LLM local attribue un type parmi une liste fermée et extrait les champs
utiles : titre, émetteur, date du document, date d'expiration.

**Done when :** sur dix documents réels, au moins huit sont classés dans le bon
type. Le compte est tenu à la main, document par document.

### cls-2 — Corriger quand il se trompe

Le bot annonce son classement et permet de le rectifier depuis le chat.

**Done when :** une correction est appliquée, persiste après redémarrage, et le
document corrigé se retrouve par le bon terme.

### rec-1 — Demander en français

« envoie-moi mon permis », « la fiche d'impôts de 2025 ». Le bot comprend
l'intention et renvoie le fichier.

**Done when :** dix demandes formulées naturellement, dont aucune ne reprend le
nom du fichier, renvoient le bon document.

### rec-2 — Ne jamais deviner en silence

Quand plusieurs documents correspondent, ou qu'aucun ne correspond
franchement, le bot propose des candidats plutôt que d'envoyer le mauvais
fichier.

**Done when :** une demande ambiguë (« le truc pour la voiture », avec permis
et carte grise en base) déclenche un choix, pas un envoi au hasard.

### exp-1 — Prévenir avant expiration

Un passage quotidien sur les dates d'expiration connues, et un message quand
une échéance approche.

**Done when :** un document dont la date d'expiration est proche déclenche un
message, sans que je l'aie demandé.

### ops-1 — Ne pas perdre mes papiers

Ce sont des pièces d'identité et des documents fiscaux. Le volume doit être
sauvegardé hors du serveur.

**Done when :** j'ai restauré la base et les fichiers depuis une sauvegarde, sur
un dossier vide, et le bot retrouve les documents.

---

## V2 — plus tard

Repoussé volontairement, pour que la V1 reste finissable :

- import en masse de l'historique
- recherche sémantique (au-delà du plein texte)
- interface web
- autres sources que Telegram (email, scanner)

---

## Hors périmètre

- **Exposition sur Internet.** Tout passe par Tailscale.
- **Chiffrement au repos.** Le disque du serveur n'est pas chiffré ; ajouter du
  chiffrement applicatif donnerait une fausse impression de sécurité sans
  traiter la vraie surface.
- **Multi-utilisateur.** Un seul `chat_id`, le mien.

---

## Ce qu'on sait déjà qui sera dur

- **Le langage libre est le vrai risque de la V1.** Un modèle 4-8B sur 6 Go de
  VRAM comprendra « mon permis », moins sûrement « le papier pour la voiture ».
  `rec-2` existe pour que l'échec soit visible et rattrapable, au lieu d'un
  mauvais fichier envoyé en silence.
- **Telegram n'est pas chiffré de bout en bout pour les bots.** C'est le seul
  point où ces documents sortent du serveur. Assumé, pas oublié.
