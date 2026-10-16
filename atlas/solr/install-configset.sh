#!/bin/bash
# Runs on every container start (image entrypoint hook). The solrdata volume
# hides anything baked into /var/solr, and Solr looks for configsets under its
# home (/var/solr/data/configsets), so copy the configset in here.
set -e
mkdir -p /var/solr/data/configsets
rm -rf /var/solr/data/configsets/omop_vocab
cp -R /opt/omop_vocab_configset /var/solr/data/configsets/omop_vocab
