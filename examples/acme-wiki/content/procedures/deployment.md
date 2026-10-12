# Deployment

Production deployments go through a [GitLab](../tools/gitlab.md) pipeline, after a code review
by a second person.

1. Merge the merge request into `main`.
2. Wait for the pipeline to pass, then click **Deploy to production**.
3. Announce it in `#changes`, with a link to the pipeline.
