ALTER TABLE hrm.cooling_centers ALTER COLUMN capacity DROP NOT NULL;
ALTER TABLE hrm.cooling_centers ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}';
ALTER TABLE hrm.dataset_versions ADD COLUMN roads jsonb;
ALTER TABLE hrm.dataset_versions ADD COLUMN inventory_status text NOT NULL DEFAULT 'unknown'
 CHECK (inventory_status IN ('unknown','partial','verified','confirmed_absent'));
