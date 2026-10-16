# syntax=docker/dockerfile:1
# Solr 8 (not 9: the DataImportHandler used to pull concepts from Postgres
# was removed in 9) with the Postgres JDBC driver and the omop_vocab configset.
FROM solr:8.11.4
USER root
# solrconfig.xml loads every jar in contrib/dataimporthandler-extras/lib.
ADD --chmod=644 https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar \
    /opt/solr/contrib/dataimporthandler-extras/lib/postgresql-42.7.4.jar
COPY conf /opt/omop_vocab_configset/conf
# Copied into the (volume-backed) Solr home at container start.
COPY --chmod=755 install-configset.sh /docker-entrypoint-initdb.d/install-configset.sh
USER solr
