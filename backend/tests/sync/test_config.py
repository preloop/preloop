"""Tests for the config module."""

import unittest
from unittest.mock import patch

from preloop.sync.config import (
    DATABASE_URL,
    LOG_LEVEL,
    SERVICE_PORT,
)


class TestConfig(unittest.TestCase):
    """Test the config module."""

    def test_config_variables(self):
        """Test configuration variables are set correctly."""
        # We can at least check that the variables exist and have the correct types
        self.assertIsNotNone(DATABASE_URL)
        self.assertIsNotNone(LOG_LEVEL)
        self.assertIsInstance(SERVICE_PORT, int)  # Should be converted to int

    @patch("preloop.sync.config.preloop_logging.configure_logging")
    @patch("preloop.sync.config._logging")
    def test_logger_setup(self, mock_logging, mock_configure_logging):
        """Test logger setup configuration."""
        # Import inside the test to use the mocked logging
        from preloop.sync.config import setup_logging

        mock_logging.INFO = 20

        # Call the function directly
        logger = setup_logging()

        mock_configure_logging.assert_called_once()
        # Verify that logging.getLogger was called
        mock_logging.getLogger.assert_called()
        # Verify that logger.setLevel was called
        mock_logging.getLogger.return_value.setLevel.assert_called()

        # Verify that a logger is returned
        self.assertEqual(logger, mock_logging.getLogger.return_value)
