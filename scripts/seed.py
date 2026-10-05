from api.store import Store, engine_for, seed

if __name__ == "__main__":
    seed(Store(engine_for()))
    print("Nigeria catalogue seeded; no legal full text imported.")
