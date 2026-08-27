REVOKE ALL PRIVILEGES ON checkpoints, checkpoint_blobs,
    checkpoint_writes, checkpoint_migrations FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON checkpoints,
    checkpoint_blobs, checkpoint_writes TO omics_app;
