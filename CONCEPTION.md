# CONCEPTION — pieces

Comment la V1 est construite. `INTENT.md` dit **jusqu'où** ; ce fichier dit
**comment**, et ce qui a été écarté.

## Les types de documents

Liste fermée. Le classement choisit exactement un type, ou `autre`.

| type | ce qu'il couvre |
| :--- | :--- |
| `identite` | CNI, passeport, titre de séjour |
| `permis_conduire` | permis, attestation de points |
| `vehicule` | carte grise, assurance auto, contrôle technique |
| `impots` | avis d'imposition, déclarations |
| `banque` | RIB, relevés, attestations |
| `sante` | carte vitale, mutuelle, ordonnances, résultats |
| `logement` | bail, quittances, assurance habitation |
| `emploi` | CV, contrats, fiches de paie, attestations |
| `diplome` | diplômes, relevés de notes, certifications |
| `transport` | billets d'avion, de train, réservations |
| `facture` | achats, abonnements, services |
| `autre` | le reste |

Douze entrées, découpées **par objet** plutôt que par nature juridique. C'est
pourquoi « assurance » n'est pas un type : une assurance auto se cherche avec la
voiture, une assurance habitation avec le logement. Un type `assurance` aurait
créé trois frontières floues au lieu d'une catégorie utile.

La liste sera réajustée après le passage des dix premiers documents réels
(critère `cls-1`), pas avant : on ne sait pas encore où le modèle hésite.

## Le modèle de données

SQLite, un seul fichier dans le volume.

```sql
CREATE TABLE documents (
  id             INTEGER PRIMARY KEY,
  sha256         TEXT NOT NULL UNIQUE,   -- dédoublonnage (ing-1)
  fichier        TEXT NOT NULL,          -- nom d'origine, tel que reçu
  chemin         TEXT NOT NULL,          -- relatif au volume
  type           TEXT NOT NULL DEFAULT 'autre',
  titre          TEXT NOT NULL DEFAULT '',
  emetteur       TEXT,
  date_document  TEXT,                   -- ISO 8601, NULL si introuvable
  date_expiration TEXT,                  -- ISO 8601, NULL si sans objet
  texte          TEXT NOT NULL DEFAULT '',
  source_texte   TEXT NOT NULL DEFAULT 'aucun'
                 CHECK (source_texte IN ('aucun', 'natif', 'ocr')),
  classe_par     TEXT NOT NULL DEFAULT 'aucun'
                 CHECK (classe_par IN ('aucun', 'llm', 'humain')),
  recu_le        TEXT NOT NULL
);

CREATE TABLE corrections (
  id          INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  champ       TEXT NOT NULL,
  avant       TEXT,
  apres       TEXT NOT NULL,
  corrige_le  TEXT NOT NULL
);

CREATE INDEX documents_type ON documents(type);
CREATE INDEX documents_expiration ON documents(date_expiration)
  WHERE date_expiration IS NOT NULL;               -- exp-1

CREATE VIRTUAL TABLE documents_fts USING fts5(
  titre, emetteur, texte,
  content='documents', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

-- En mode `content=`, FTS5 ne se met pas à jour tout seul : sans ces trois
-- déclencheurs l'index reste vide, et ça ne se voit qu'à rec-1 sous la forme
-- « la recherche ne trouve jamais rien ».
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, titre, emetteur, texte)
  VALUES (new.id, new.titre, new.emetteur, new.texte);
END;

CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, titre, emetteur, texte)
  VALUES ('delete', old.id, old.titre, old.emetteur, old.texte);
END;

CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, titre, emetteur, texte)
  VALUES ('delete', old.id, old.titre, old.emetteur, old.texte);
  INSERT INTO documents_fts(rowid, titre, emetteur, texte)
  VALUES (new.id, new.titre, new.emetteur, new.texte);
END;
```

`remove_diacritics 2` fait que « impots » trouve « impôts ». Sans ça, la moitié
des recherches en français échoue sur un accent.

### Un document arrive incomplet, et c'est prévu

Les valeurs par défaut ne sont pas de la complaisance : elles viennent de
l'ordre des stories. Quand `sec-1 + ing-1` range son premier PDF, il n'a
traversé ni l'extraction (`ext-1`) ni le classement (`cls-1`) — il n'a donc ni
`texte`, ni `type`, ni `titre`. Sans défauts, la première story ne pourrait
insérer aucune ligne valide, et le dédoublonnage qu'elle promet n'existerait
pas.

Une ligne fraîche est donc un document connu mais pas encore compris :
`type = 'autre'`, `texte = ''`, `source_texte = 'aucun'`, `classe_par =
'aucun'`. Le `titre` prend le nom du fichier — il vaut mieux qu'une chaîne vide
le jour où l'on liste les documents, et le classement l'écrasera.

`'aucun'` est une troisième valeur assumée pour `source_texte` et `classe_par`,
là où le premier jet n'en prévoyait que deux. Elle dit « pas encore traité »,
ce qu'un `NULL` dirait moins clairement et qu'une valeur par défaut mensongère
(`'natif'`, `'llm'`) rendrait indétectable.

Le choix a été de rester sur des colonnes à défaut plutôt que d'introduire une
colonne d'état et une machine à états. À ce volume, l'état se lit déjà dans les
données : `source_texte = 'aucun'` *est* « pas encore extrait ».

Les `CHECK` portent sur `source_texte` et `classe_par`, jamais sur `type` : ces
deux-là sont des constantes techniques, alors que la liste des types sera
réajustée après les dix premiers documents réels. Un `CHECK` sur `type` ferait
payer chaque ajustement d'une migration de table entière.

**L'historique des corrections est conservé** (`cls-2`). Le coût est nul et
c'est le seul matériau qui dira, dans trois mois, sur quels types le modèle se
trompe vraiment. Sans cette table, on ajusterait le prompt à l'aveugle.

`type` n'est pas indexé en plein texte : il sert de filtre SQL, pas de terme de
recherche.

La base est ouverte en **WAL** dès sa création. Le passage quotidien des
expirations (`exp-1`) lira pendant que le bot écrit ; en mode journal par
défaut, l'un des deux se prendrait un `database is locked`. Le mode est une
propriété persistante du fichier, donc à poser une fois, au début, pas à
rattraper le jour où la lecture échoue.

Chaque opération ouvre et referme sa connexion. À une écriture par document et
quelques lectures par jour, le coût est invisible, et ça évite d'avoir à
protéger une connexion partagée entre la boucle asyncio et les threads.

## Le stockage des fichiers

```
/data/documents/<2 premiers car. du sha>/<sha256>.pdf
/data/pieces.db
```

Le nom du fichier **est** son empreinte : le dédoublonnage devient une
propriété du système de fichiers, pas une règle à faire respecter. Le nom
d'origine vit en base, où il peut changer sans rien déplacer.

## Les modules

```
src/
  config.py      lit l'environnement, échoue au démarrage si incomplet
  db.py          schéma, migrations, accès
  storage.py     empreinte et écriture des PDF dans le volume
  extract.py     texte natif, puis OCR en secours
  classify.py    appel au LLM, sortie contrainte
  search.py      demande en français → requête structurée → résultats
  bot.py         Telegram : whitelist, réception, réponses
  reminders.py   passage quotidien sur les expirations
  main.py        assemblage
```

`config.py` échoue **au démarrage**, pas au premier message : un bot lancé sans
son token doit mourir tout de suite, pas trois heures plus tard devant un
document.

## Le transport Telegram

**Long polling, pas de webhook.** Ce n'est pas un arbitrage : rien n'est exposé
sur Internet, donc Telegram n'a aucune adresse où livrer un webhook. Le bot sort
vers `api.telegram.org` et redemande ses messages. C'est exactement la règle du
homelab — les services sortent, jamais l'inverse.

**aiogram** plutôt que `python-telegram-bot`, pour une raison précise : son
`outer_middleware` sur le dispatcher voit passer *tous* les types d'updates en
un seul point. C'est ce qu'exige la whitelist ci-dessous.

### La whitelist

Elle est posée en middleware sur le dispatcher, pas en garde au début de chaque
handler. La différence compte : `cls-2` (corriger un classement) et `rec-2`
(choisir parmi des candidats) amèneront des boutons, donc des `callback_query` —
un type d'update avec son propre chemin d'arrivée. Un garde recopié handler par
handler y serait oublié, et la faille reviendrait à la story suivante. Posé une
fois sur le dispatcher, il couvre ce qui n'est pas encore écrit.

Le middleware ne connaît d'ailleurs aucun type d'update en particulier : il lit
l'événement porté par l'`Update`, quel qu'il soit, et y cherche un expéditeur.
Un type ajouté par Telegram demain est donc refusé par défaut, pas ignoré.

**Le filtre porte sur l'expéditeur, pas sur le `chat_id`.** Les deux coïncident
en conversation privée, et c'est ce qui rend la confusion facile : dans un
groupe, `chat_id` est celui du groupe, où n'importe qui parle. Un compte
autorisé y demanderait son permis devant des tiers. D'où deux conditions :
l'expéditeur est dans la liste, **et** le chat est privé.

**Un refus est silencieux.** Répondre « non autorisé » confirmerait l'existence
du bot à qui le sonde. Le refus ne laisse qu'une ligne de log, côté serveur —
et c'est précisément cette ligne qui rend le critère de `sec-1 + ing-1`
vérifiable : sans elle, « refusé correctement » et « le bot est planté » se
ressemblent trop.

L'update refusé est tout de même consommé. Un update qu'on n'acquitte pas est
redemandé indéfiniment par Telegram.

### Le pipeline d'un document

```
PDF reçu
   ↓  extension, type MIME, taille annoncée (20 Mo max)
téléchargement
   ↓  les premiers octets sont-ils vraiment ceux d'un PDF ?
SHA-256
   ↓  déjà en base ? → on le dit, on s'arrête là
écriture du fichier, puis insertion en base
```

L'ordre de ces deux dernières étapes n'est pas indifférent. Le fichier
d'abord : si le conteneur meurt entre les deux, un fichier sans ligne est
inoffensif — le prochain envoi du même document le retrouve et complète la
base. Une ligne pointant vers un fichier absent, elle, est une entrée cassée
qu'on découvre le jour où on demande le document.

L'écriture passe par un temporaire puis un `rename` : un fichier présent sous
son nom définitif est toujours complet.

## Le classement

Appel à Ollama avec une sortie contrainte par schéma JSON — type parmi la liste
fermée, titre, émetteur, dates. Le modèle ne rédige pas de prose : il remplit
des cases.

Le texte envoyé est tronqué aux premiers milliers de caractères. Sur ces
documents, le type se décide dans l'en-tête ; envoyer quarante pages sature la
fenêtre de contexte sans rien apporter.

Modèle par défaut configurable (`OLLAMA_MODEL`). Avec 6 Go de VRAM, un 4B en Q4
est le point de départ raisonnable ; on mesurera contre le critère 8/10 de
`cls-1` avant de monter à un 8B.

**Ce que le schéma garantit, et ce qu'il ne garantit pas.** Ollama traduit le
schéma en grammaire : un type hors de la liste fermée est impossible, pas
seulement improbable. En revanche la grammaire ne contraint que la *forme* —
rien n'empêche le modèle de rendre « mars 2024 », « 12/03/2024 » ou un numéro
de dossier dans un champ déclaré date. Les dates sont donc revalidées à
l'arrivée : normalisées en ISO 8601, vérifiées comme dates existantes
(le 31 février est refusé), et rejetées hors d'une plage plausible. Une date
douteuse vaut mieux absente qu'approximative — `exp-1` enverra des rappels à
partir de cette colonne.

`date_expiration` exige en plus le jour, là où `date_document` accepte une
année seule : une expiration se compare à aujourd'hui, ce qu'une année ne
permet pas. Une année de document, elle, suffit à répondre à « la fiche
d'impôts de 2025 ».

**Les champs absents reviennent en chaîne vide, pas en `null`.** Une union
`["string", "null"]` se traduit mal en grammaire ; la chaîne vide est sans
ambiguïté et devient `NULL` à la conversion, juste ici. Un `titre` vide, lui,
n'écrase pas celui qui est en base : le nom du fichier reste un meilleur repère
que rien.

**Pas de colonne « classement tenté »**, contrairement à l'OCR. Les deux
situations n'ont pas le même coût : l'OCR se paie en minutes et en appels
distants, le classement est local et se compte en secondes. Un document que le
modèle ne sait pas nommer atterrit dans `autre` avec `classe_par = 'llm'`, donc
hors du rattrapage ; seule une panne d'Ollama le laisse à `'aucun'`, et c'est
exactement ce qu'on veut reprendre au démarrage suivant.

**Ollama n'est pas exigé au démarrage**, contrairement au jeton Telegram et à
la clé d'OCR. Un Ollama éteint laisse les documents rangés et lisibles,
simplement pas encore classés ; mourir au démarrage ferait payer au stockage
une panne du classement. Le rattrapage s'arrête en revanche au premier document
dès qu'Ollama ne répond pas : sans ça, un serveur sans modèle attendrait le
délai d'expiration pour chacun de ses documents, à chaque démarrage.

**Le prompt a été mesuré, pas deviné.** Une première version, plus courte, se
contentait de lister les types et de dire « n'invente rien ». Sur les quatre
premiers documents réels, elle rendait `autre` pour un CV — alors que le type
`emploi` cite explicitement les CV — et inventait une date d'expiration pour ce
CV comme pour une attestation, deux documents qui n'en portent aucune.

Deux ajouts ont corrigé les deux défauts, vérifiés sur les mêmes documents :

- *« autre est un dernier recours : ne l'emploie que si aucun des onze autres
  ne s'applique »*, avec le CV en exemple. Un modèle de cette taille prend
  `autre` pour une réponse acceptable quand il hésite ; il faut lui dire que
  non.
- pour `date_expiration`, la liste de ce qui n'expire pas (CV, facture, avis
  d'imposition, relevé, justificatif) et la consigne de ne remplir que si le
  document dit lui-même jusqu'à quand il vaut.

| | types corrects | dates d'expiration inventées |
| :-- | :-- | :-- |
| premier prompt | 3/4 | 2 |
| prompt mesuré | 4/4 | 0 |

La vraie date d'expiration, celle de la carte grise, est conservée dans les
deux cas : la consigne n'a pas rendu le modèle muet, elle l'a rendu prudent.
Quatre documents ne font pas une mesure — c'est le compte de dix qui tranche —
mais ils ont suffi à voir les deux défauts.

**Où vit Ollama.** Dans le dépôt du homelab, pas ici : c'est une brique de la
machine, partagée, pas un composant de `pieces`. Il est joint par son nom de
conteneur sur le réseau `homelab`, et n'expose aucun port — le classement est
le seul traitement de la V1 qui ne sort pas du serveur, et il n'y a aucune
raison de lui ouvrir une porte.

## La recherche, en deux étages

C'est le point le plus délicat de la V1 (`rec-1`, `rec-2`).

```
« envoie-moi la fiche d'impôts de 2025 »
            ↓
   étage 1 — le LLM traduit l'intention
            {type: "impots", termes: ["fiche"], annee: 2025}
            ↓
   étage 2 — SQL + FTS5 exécutent et classent
            ↓
   0 résultat → on le dit    1 résultat → on l'envoie
   plusieurs  → on propose le choix (rec-2)
```

**Ce que le plein texte ne fait pas.** Mesuré sur le schéma réel : les accents
sont bien neutralisés — « impots » trouve « impôts », la casse est ignorée —
mais FTS5 ne fait **aucune racinisation**. « impot » ne trouve pas « impôts »,
« revenu » ne trouve pas « revenus ». L'étage 1 devra donc produire des termes
à préfixe (`impot*`) ou proposer les deux nombres, sinon une demande au
singulier manquera un document au pluriel. À trancher à `rec-1`, avec de vraies
formulations plutôt qu'en devinant.

**Le LLM traduit, il ne choisit pas.** Le document renvoyé est déterminé par une
requête SQL, donc reproductible et débogable : devant un mauvais résultat, on
lit la requête produite et on sait lequel des deux étages a fauté.

L'alternative — donner la liste des documents au modèle et le laisser désigner
le bon — a été écartée : elle ne passe pas l'échelle de la fenêtre de contexte,
et surtout elle rend l'erreur inexplicable.

## L'extraction

`pdftotext` d'abord, en local. En dessous d'un seuil de texte utile, le
document est traité comme un scan et part à l'**OCR de Mistral**.
`source_texte` garde la trace du chemin emprunté — utile quand un document est
mal classé pour savoir si c'est l'OCR ou le modèle qui a fauté.

**Pourquoi un OCR distant, contre le cadrage initial.** La V1 prévoyait
`ocrmypdf` en local. Il a été écrit, déployé, puis mesuré : 678 Mo d'image
contre 297 aujourd'hui, une lecture parfaite d'un scan droit, et plus rien du
tout à partir d'une douzaine de degrés d'inclinaison — `--deskew` et
`--rotate-pages` n'y changeaient rien, les quatre variantes d'options rendaient
un résultat identique au caractère près. Or un document photographié au
téléphone est rarement droit.

Le même document passé à Mistral se lit intégralement, droit comme à douze
degrés, en moins d'une seconde. Le compromis a donc été refait : la qualité de
lecture et 380 Mo d'image contre un second point de sortie des documents. C'est
le seul appel externe du projet, et il est délibéré.

**Une panne n'est pas un document illisible.** L'appel distingue trois issues :
lu (`ocr`), illisible (`aucun`), et injoignable (`indisponible` — réseau coupé,
quota atteint, clé refusée). Seules les deux premières closent la question ;
la troisième laisse le document en attente pour le prochain démarrage. Sans
cette distinction, une coupure de réseau d'une minute condamnerait un document
à ne jamais être lu.

L'appel se fait avec **`-layout`**, qui préserve colonnes et tableaux. Un avis
d'imposition dont les colonnes fusionnent perd le lien entre un libellé et son
montant — précisément ce que le classement devra lire.

Un PDF sans couche texte ne produit **pas une erreur** : il repart avec
`source_texte = 'aucun'`, qui est l'état « en attente d'OCR ». Le seuil qui
déclenche ce verdict est grossier, et c'est voulu : il sera réglé à `ext-2`,
avec de vrais scans sous la main plutôt qu'au jugé. Un `pdftotext` absent,
en échec ou trop lent aboutit au même état — le document reste rangé, et c'est
ce qui compte.

**Le rattrapage.** Au démarrage, les documents à `source_texte = 'aucun'`
repassent par l'extraction. Sans ce passage, un document rangé avant cette
story resterait muet pour toujours : le renvoyer ne servirait à rien, le
dédoublonnage l'écarterait avant d'y toucher. C'est aussi ce qui rattrape un
document arrivé juste avant un arrêt. Un scan y repasse à chaque démarrage sans
rien produire, ce qui est sans conséquence à ce volume — et cessera dès que
l'OCR saura le lire.

## Alternatives écartées

**Paperless-ngx.** Fait déjà tout ça, et bien : OCR, classement, recherche,
interface web, mature. À considérer honnêtement si l'objectif était seulement
de ranger des documents. Écarté parce que ce qui est voulu ici, c'est
l'interface Telegram et la demande en français — et parce que c'est un projet
qu'on veut construire, pas seulement utiliser.

**Postgres + pgvector.** Surdimensionné pour quelques centaines de documents
qui arrivent au compte-gouttes. SQLite tient, se sauvegarde en copiant un
fichier, et ne demande aucun service supplémentaire.

**Recherche sémantique par embeddings.** Repoussée en V2. Le plein texte avec
suppression des accents couvre la majorité des demandes ; ajouter des
embeddings avant d'avoir mesuré ce qui échoue, c'est optimiser à l'aveugle.

**Un type `assurance`.** Voir plus haut : trois frontières floues au lieu d'une
catégorie utile.
