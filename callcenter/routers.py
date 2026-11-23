class ReadOnlySecondaryRouter:
    def db_for_write(self, models, **hints):
        return default

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "secondary":
            return False
        return None
