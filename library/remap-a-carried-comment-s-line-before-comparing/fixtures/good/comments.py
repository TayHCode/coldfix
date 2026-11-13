def carry_forward(comment, head):
    return dict(comment, commit=head, original_line=None)
