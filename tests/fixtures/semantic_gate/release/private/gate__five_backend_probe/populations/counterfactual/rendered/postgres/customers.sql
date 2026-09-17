-- deterministic load script for customers (elt-taskgen)
DROP TABLE IF EXISTS "customers";
CREATE TABLE "customers" (
  "customer_id" INTEGER NOT NULL,
  "customer_name" TEXT NOT NULL,
  "segment" TEXT,
  PRIMARY KEY ("customer_id")
);
INSERT INTO "customers" ("customer_id", "customer_name", "segment") VALUES
(901, 'Ada', 'gold'),
(902, 'Bob', NULL),
(903, 'Cy', 'null'),
(904, 'Di', ''),
(905, 'Ed', 'None');
