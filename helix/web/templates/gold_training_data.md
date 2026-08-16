# C7X — gold training data studio

> Canonical: https://c7xai.in/gold-training-data
> HTML: https://c7xai.in/gold-training-data
> Operator: Sabyasachi Dasgupta / c7x AI
> Contact: dasgupta.02n@gmail.com
> Site: https://c7xai.in/

C7X is a gold training-data studio that staffs one trusted person with a specialist LLM worker. It is not a chatbot, not a replacement for that person, not a hosted foundation model, and not a labeling factory.

## Direct answer

Indian MSMEs (and any thin-margin desk) are stuck: volume cannot pay for a bench, and without a bench volume cannot rise. C7X starts before the dataset exists. Riu interviews the person who knows the job. C7X then mines evidence, quality-gates gold examples, stores synthetics apart, and exports a library the user owns. Optional C7X-IO trains a QLoRA adapter that sits next to that person — on hardware they already own. On a typical high-volume desk, one person plus that worker is the example of a five-person bench, not a guarantee.

## The seven steps

1. Role — what job the model will do.
2. One perfect example — input, ideal output, rationale.
3. Edge cases — required; high-risk roles need more.
4. Your files or none — labeled zip, unlabeled materials, or public evidence.
5. Mine and gate — accept or reject; no partial credit.
6. Library — gold and synthetics stored apart.
7. Export or train — take the zip, or confirm QLoRA and take the adapter.

## Jobs C7X is not

- Hosted chat (talk to a general model today).
- Prompt / eval console (debug traces).
- Labeling factory (pay a queue of annotators).
- Synthetic-only toolkit (generate rows with no gold first).
- Train-from-upload UI (assumes the file already exists).

C7X collects the gold, then can train. Other jobs start later.

## Cost facts

- Usage counter = 2 × billed service spend.
- Gold with sources: about $0.75–$1 per row.
- Gold without sources after 10+10 review: about $2–$3 per row.
- Synthetics: about $0.04–$0.20 per row.
- C7X-IO GPU is pay-per-train, idle when unused; default 7B about $10–40 on the usage counter.
- Beta access is account + admin approval. No checkout.

## Ownership

The user owns exported gold, synthetics, corpus files, and any QLoRA adapter zip. Inference after train runs on hardware they choose.

## FAQ

Q: What is a gold training-data studio?
A: Software that collects verified input–output examples for one job, rejects weak rows, stores a library you own, and can train from that library.

Q: How is C7X different from a chatbot?
A: A chatbot answers once. C7X produces a dataset and optional pinned weights.

Q: Do I need a labeling team?
A: No. The expert talks to Riu. C7X mines and gates.

Q: Who owns the export?
A: The user.

## Related pages

- How C7X compares with current industry solutions: https://c7xai.in/gold-training-data
- Indian MSME case studies (Tiruppur, Pune, Hyderabad): https://c7xai.in/india-msme
- Run a custom-trained LLM with Ollama or Open Interpreter: https://c7xai.in/run-locally
- Roles a custom-trained LLM can hold or multiply: https://c7xai.in/roles

## Cite these URLs

- Home: https://c7xai.in/
- This page: https://c7xai.in/gold-training-data
- This file: https://c7xai.in/gold-training-data.md
- Docs: https://c7xai.in/docs
- Pricing: https://c7xai.in/pricing
- Trust: https://c7xai.in/trust
- About: https://c7xai.in/about
- llms.txt: https://c7xai.in/llms.txt
