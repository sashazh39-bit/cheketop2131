import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.pdmodel.font.PDType0Font;
import java.io.*;

public class FontTest {
    public static void main(String[] args) throws Exception {
        String fontPath = args[0];
        
        PDDocument doc = new PDDocument();
        PDType0Font font = PDType0Font.load(doc, new File(fontPath));
        
        System.out.println("Font loaded: " + font.getName());
        System.out.println("BaseFont: " + font.getBaseFont());
        
        String testChars = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЫЭЮЯабвгдежзиклмнопрстуфхцчшщыэюя0123456789";
        
        for (char c : testChars.toCharArray()) {
            try {
                float w = font.getStringWidth(String.valueOf(c));
                byte[] encoded = font.encode(String.valueOf(c));
                int cid = -1;
                if (encoded.length >= 2) {
                    cid = ((encoded[0] & 0xFF) << 8) | (encoded[1] & 0xFF);
                }
                System.out.printf("  %c (U+%04X): width=%.1f, CID=%d (0x%04X), encoded=%d bytes%n", 
                    c, (int)c, w, cid, cid, encoded.length);
            } catch (Exception e) {
                System.out.printf("  %c (U+%04X): ERROR: %s%n", c, (int)c, e.getMessage());
            }
        }
        
        doc.close();
    }
}
