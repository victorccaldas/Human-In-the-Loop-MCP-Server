"""Regression tests for tkinter markdown table support.

Verifies that Markdown tables are correctly parsed and rendered in GUI dialogs.
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import human_loop_server


def test_is_table_separator_row():
    """Test table separator detection."""
    # Valid separators
    assert human_loop_server._is_table_separator_row("| --- | --- |")
    assert human_loop_server._is_table_separator_row("| :--- | :---: | ---: |")
    assert human_loop_server._is_table_separator_row("|---|---|")
    assert human_loop_server._is_table_separator_row("| ----- | ----- |")
    
    # Invalid separators
    assert not human_loop_server._is_table_separator_row("| abc | def |")
    assert not human_loop_server._is_table_separator_row("not a table")
    assert not human_loop_server._is_table_separator_row("")


def test_parse_table_row():
    """Test table row parsing into cells."""
    assert human_loop_server._parse_table_row("| Name | Age | City |") == ["Name", "Age", "City"]
    assert human_loop_server._parse_table_row("| Alice | 30 | NYC |") == ["Alice", "30", "NYC"]
    assert human_loop_server._parse_table_row("|  spaced  |  values  |") == ["spaced", "values"]
    assert human_loop_server._parse_table_row("| single |") == ["single"]


def test_prompt_looks_like_markdown_detects_tables():
    """_prompt_looks_like_markdown should detect table syntax."""
    assert human_loop_server._prompt_looks_like_markdown("| Col1 | Col2 |\n| --- | --- |\n| A | B |")
    assert human_loop_server._prompt_looks_like_markdown("Some text\n\n| Header |\n| --- |\n| Data |")


def test_render_table_with_valid_table():
    """_render_table should correctly render a Markdown table."""
    mock_widget = Mock()
    mock_widget.insert = Mock()
    
    lines = [
        "| Name | Age | City |",
        "| --- | --- | --- |",
        "| Alice | 30 | NYC |",
        "| Bob | 25 | LA |",
    ]
    
    theme_colors = {
        "bg_accent": "#f0f0f0",
        "fg_primary": "#000000",
    }
    
    consumed = human_loop_server._render_table(mock_widget, lines, theme_colors)
    
    assert consumed == 4  # All 4 lines consumed
    assert mock_widget.insert.call_count > 0
    
    # Verify header, separator, and data rows were inserted
    calls = [str(call) for call in mock_widget.insert.call_args_list]
    combined = "".join(calls)
    assert "Name" in combined
    assert "Alice" in combined
    assert "Bob" in combined


def test_render_table_with_invalid_input():
    """_render_table should return 0 for invalid input."""
    mock_widget = Mock()
    
    # Not enough lines
    assert human_loop_server._render_table(mock_widget, ["| Header |"], {}) == 0
    
    # No separator
    lines = ["| Header |", "| Data |"]
    assert human_loop_server._render_table(mock_widget, lines, {}) == 0
    
    # First line not a table
    lines = ["Not a table", "| --- |"]
    assert human_loop_server._render_table(mock_widget, lines, {}) == 0


def test_render_table_with_variable_columns():
    """_render_table should handle rows with variable column counts."""
    mock_widget = Mock()
    mock_widget.insert = Mock()
    
    lines = [
        "| Col1 | Col2 | Col3 |",
        "| --- | --- | --- |",
        "| A | B |",  # Missing third column
        "| X | Y | Z | Extra |",  # Extra column (should be truncated)
    ]
    
    theme_colors = {"bg_accent": "#f0f0f0"}
    
    consumed = human_loop_server._render_table(mock_widget, lines, theme_colors)
    
    assert consumed == 4
    # Should not crash and should handle the mismatch gracefully


def test_set_prompt_text_content_with_table():
    """_set_prompt_text_content should render tables when markdown is detected."""
    mock_widget = Mock()
    mock_widget.configure = Mock()
    mock_widget.delete = Mock()
    mock_widget.insert = Mock()
    mock_widget.index = Mock(return_value="1.0")
    mock_widget.tag_add = Mock()
    mock_widget.tag_configure = Mock()
    
    prompt = """
# Report

Here's a summary:

| Name | Status | Count |
| --- | --- | --- |
| Task A | Done | 5 |
| Task B | Pending | 3 |

End of report.
"""
    
    theme_colors = human_loop_server.get_theme_colors()
    
    with patch("human_loop_server.get_system_font", return_value=("Arial", 10)):
        with patch("human_loop_server.get_text_font", return_value=("Courier", 9)):
            human_loop_server._set_prompt_text_content(mock_widget, prompt, theme_colors)
    
    # Verify content was inserted
    assert mock_widget.insert.call_count > 0
    
    # Check that table content appears in insert calls
    calls = [str(call) for call in mock_widget.insert.call_args_list]
    combined = "".join(calls)
    assert "Task A" in combined
    assert "Task B" in combined


def test_table_rendering_does_not_break_other_markdown():
    """Tables should coexist with other markdown features."""
    mock_widget = Mock()
    mock_widget.configure = Mock()
    mock_widget.delete = Mock()
    mock_widget.insert = Mock()
    mock_widget.index = Mock(return_value="1.0")
    mock_widget.tag_add = Mock()
    mock_widget.tag_configure = Mock()
    
    prompt = """
## Heading

This is **bold** and *italic*.

| Col1 | Col2 |
| --- | --- |
| A | B |

- List item 1
- List item 2

> Blockquote

End.
"""
    
    theme_colors = human_loop_server.get_theme_colors()
    
    with patch("human_loop_server.get_system_font", return_value=("Arial", 10)):
        with patch("human_loop_server.get_text_font", return_value=("Courier", 9)):
            human_loop_server._set_prompt_text_content(mock_widget, prompt, theme_colors)
    
    # Should not raise any exceptions
    assert mock_widget.insert.call_count > 0
