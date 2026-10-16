-- Keys and indexes from the OMOP CDM v5.4 Postgres scripts, minus CLUSTER
-- (it rewrites each table, doubling disk use) and minus single-column
-- indexes that duplicate a primary key.
ALTER TABLE vocab.concept ADD CONSTRAINT xpk_concept PRIMARY KEY (concept_id);
ALTER TABLE vocab.vocabulary ADD CONSTRAINT xpk_vocabulary PRIMARY KEY (vocabulary_id);
ALTER TABLE vocab.domain ADD CONSTRAINT xpk_domain PRIMARY KEY (domain_id);
ALTER TABLE vocab.concept_class ADD CONSTRAINT xpk_concept_class PRIMARY KEY (concept_class_id);
ALTER TABLE vocab.relationship ADD CONSTRAINT xpk_relationship PRIMARY KEY (relationship_id);

CREATE INDEX idx_concept_code ON vocab.concept (concept_code);
CREATE INDEX idx_concept_vocabluary_id ON vocab.concept (vocabulary_id);
CREATE INDEX idx_concept_domain_id ON vocab.concept (domain_id);
CREATE INDEX idx_concept_class_id ON vocab.concept (concept_class_id);
CREATE INDEX idx_concept_relationship_id_1 ON vocab.concept_relationship (concept_id_1);
CREATE INDEX idx_concept_relationship_id_2 ON vocab.concept_relationship (concept_id_2);
CREATE INDEX idx_concept_relationship_id_3 ON vocab.concept_relationship (relationship_id);
CREATE INDEX idx_concept_synonym_id ON vocab.concept_synonym (concept_id);
CREATE INDEX idx_concept_ancestor_id_1 ON vocab.concept_ancestor (ancestor_concept_id);
CREATE INDEX idx_concept_ancestor_id_2 ON vocab.concept_ancestor (descendant_concept_id);
CREATE INDEX idx_drug_strength_id_1 ON vocab.drug_strength (drug_concept_id);
CREATE INDEX idx_drug_strength_id_2 ON vocab.drug_strength (ingredient_concept_id);

ANALYZE;
