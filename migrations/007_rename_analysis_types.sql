UPDATE jobs
SET analysis_type = 'deg'
WHERE analysis_type = 'differential';

UPDATE jobs
SET analysis_type = 'gma'
WHERE analysis_type = 'correlation';

UPDATE jobs
SET payload = jsonb_set(payload, '{analysis_type}', '"deg"'::jsonb, false)
WHERE payload ->> 'analysis_type' = 'differential';

UPDATE jobs
SET payload = jsonb_set(payload, '{analysis_type}', '"gma"'::jsonb, false)
WHERE payload ->> 'analysis_type' = 'correlation';
