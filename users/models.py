from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """
    Custom user model from the first day of the project.
    This keeps future business extensions safe and predictable.
    """

    pass
