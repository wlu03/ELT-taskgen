-- deterministic load script for customers (elt-taskgen)
DROP TABLE IF EXISTS "customers";
CREATE TABLE "customers" (
  "customer_id" INTEGER NOT NULL,
  "customer_name" TEXT NOT NULL,
  "segment" TEXT,
  PRIMARY KEY ("customer_id")
);
INSERT INTO "customers" ("customer_id", "customer_name", "segment") VALUES
(5000001, 'Ada', 'gold'),
(5000002, 'Bob', NULL),
(5000003, 'Cy', 'null'),
(5000004, 'Di', ''),
(5000005, 'Ed', 'None');
