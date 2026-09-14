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
  type           TEXT NOT NULL,
  titre          TEXT NOT NULL,
  emetteur       TEXT,
  date_document  TEXT,                   -- ISO 8601, NULL si introuvable
  date_expiration TEXT,                  -- ISO 8601, NULL si sans objet
  texte          TEXT NOT NULL,
  source_texte   TEXT NOT NULL,          -- 'natif' | 'ocr'
  classe_par     TEXT NOT NULL,          -- 'llm' | 'humain'
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

CREATE VIRTUAL TABLE documents_fts USING fts5(
  titre, emetteur, texte,
  content='documents', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);
```

`remove_diacritics 2` fait que « impots » trouve « impôts ». Sans ça, la moitié
des recherches en français échoue sur un accent.

**L'historique des corrections est conservé** (`cls-2`). Le coût est nul et
c'est le seul matériau qui dira, dans trois mois, sur quels types le modèle se
trompe vraiment. Sans cette table, on ajusterait le prompt à l'aveugle.

`type` n'est pas indexé en plein texte : il sert de filtre SQL, pas de terme de
recherche.

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

**Le LLM traduit, il ne choisit pas.** Le document renvoyé est déterminé par une
requête SQL, donc reproductible et débogable : devant un mauvais résultat, on
lit la requête produite et on sait lequel des deux étages a fauté.

L'alternative — donner la liste des documents au modèle et le laisser désigner
le bon — a été écartée : elle ne passe pas l'échelle de la fenêtre de contexte,
et surtout elle rend l'erreur inexplicable.

## L'extraction

`pdftotext` d'abord. En dessous d'un seuil de texte utile, le document est
traité comme un scan : `ocrmypdf` en français et anglais, puis nouvelle
extraction. `source_texte` garde la trace du chemin emprunté — utile quand un
document est mal classé pour savoir si c'est l'OCR ou le modèle qui a fauté.

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
