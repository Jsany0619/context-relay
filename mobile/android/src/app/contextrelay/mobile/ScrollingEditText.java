package app.contextrelay.mobile;

import android.content.Context;
import android.view.MotionEvent;
import android.widget.EditText;

/** Let the surrounding page scroll when this editor has no more text in that direction. */
public final class ScrollingEditText extends EditText {
    private float previousY;
    public ScrollingEditText(Context context) { super(context); }

    @Override public boolean onTouchEvent(MotionEvent event) {
        int action = event.getActionMasked();
        int direction = event.getY() > previousY ? -1 : 1;
        boolean handled = super.onTouchEvent(event);
        if (getParent() != null) {
            boolean editorCanScroll = action == MotionEvent.ACTION_DOWN
                    ? canScrollVertically(-1) || canScrollVertically(1)
                    : action == MotionEvent.ACTION_MOVE && canScrollVertically(direction);
            getParent().requestDisallowInterceptTouchEvent(editorCanScroll);
        }
        previousY = event.getY();
        return handled;
    }
}
