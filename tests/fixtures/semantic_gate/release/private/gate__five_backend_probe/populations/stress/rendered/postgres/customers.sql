-- deterministic load script for customers (elt-taskgen)
DROP TABLE IF EXISTS "customers";
CREATE TABLE "customers" (
  "customer_id" INTEGER NOT NULL,
  "customer_name" TEXT NOT NULL,
  "segment" TEXT,
  PRIMARY KEY ("customer_id")
);
INSERT INTO "customers" ("customer_id", "customer_name", "segment") VALUES
(7000001, 'Ada', 'gold'),
(7000002, 'Bob', NULL),
(7000003, 'Cy', 'null'),
(7000004, 'Di', ''),
(7000005, 'Ed', 'None');
