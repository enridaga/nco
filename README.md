# News Classification Ontology (NCO)

Documentation: https://enridaga.github.io/nco/

| Version | Folder | Documentation |
|---|---|---|
| 2.2 (latest) | [`v2.2/`](v2.2/) | [v2.2/](https://enridaga.github.io/nco/v2.2/) |
| 1.4.3 | [`archive/1.4/`](archive/1.4/) | [archive/1.4/docs/](https://enridaga.github.io/nco/archive/1.4/docs/) |

Each version folder holds the ontology as Turtle, RDF/XML, N-Triples, JSON-LD and Manchester syntax (`ontology.*`).

## Building the documentation

Term descriptions are OWL Manchester syntax frames rendered by ROBOT (OWL API).

```sh
pip install -r build/requirements.txt
curl -sSLO https://github.com/ontodev/robot/releases/download/v1.9.11/robot.jar
python build/build.py --robot robot.jar
```

This writes `v2.2/ontology.omn`, `v2.2/index.html` (and `index-en.html`, used by the `.htaccess` content negotiation) and the root `index.html` (latest version). The GitHub Action in `.github/workflows/docs.yml` runs the same build on every push that changes an ontology or the build files.

Page metadata not stored in the ontology (authors, release date, previous versions) is in `build/metadata.json`. To publish a new version, add a `vX.Y/` folder with `ontology.ttl` and the other serialisations, then update `versionDir`, `version` and `previous` there.
